# -*- coding: utf-8 -*-
"""每日预测引擎（pred-v1.0，2026-09-28 新增）

定位：把库里已有的六路信号（技术面 / 论坛情绪 / 龙虎榜资金 / 选手跟随 / 外部参考 / 筹码）
汇合成一份**每日短线推荐**（10 核心 + 10 观察），并在 T+1 / T+3 / T+5 用真实收盘价
自动复盘，按各信号的实际命中率**自适应调整权重** —— 形成一个可追溯、可验证的自我进化闭环。

设计口径（重要，界面上会一并标注）：
- 预测基准价：预测日（T）收盘价；复盘收益 = 目标日收盘 / T 日收盘 - 1（不复权口径）。
- 命中判定：该票同期收益 > **全候选池同期等权平均收益**（即"跑赢本池"），衡量的是选股超额能力，
  而不是绝对涨跌 —— 熊市里能选到跌得少的，同样算命中。
- 权重学习：贝叶斯收缩。样本少时靠近中性先验（0.5 命中率），样本多时才相信实盘表现，
  避免 3 个样本的高胜率把权重带飞（防过拟合）。
- 历史不可回补：posts / positions / lhb_* 等表只存当前状态，无历史快照 → 只有技术面可回放，
  其余信号必须从启用日起逐日实盘累积。
- K 线来源复用 strategy_sim.fetch_kline（新浪日线，空代理 opener，绕过本机代理劫持）。

对外接口（server.py 调用）：
    ensure_tables(con)          建表（幂等）
    generate(con, run_date=None, force=False)  生成当日预测 → dict
    review(con, force=False)     复盘已到期窗口 → dict
    rescore(con)                 按实盘命中率重算权重 → dict
    today_payload(con)           前端「今日推荐」载荷
    scores_payload(con)          前端「策略记分板」载荷
    track_payload(con)           前端「复盘轨迹」载荷
    backtest(days)               技术面历史回放（离线，命令行用）
"""
from __future__ import annotations

import concurrent.futures
import json
import math
import os
import sqlite3
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import strategy_sim as SS  # noqa: E402

try:
    from strategy_data import STRATEGIES  # noqa: E402
except Exception:  # noqa: BLE001
    STRATEGIES = []

ENGINE_VER = "pred-v2.0"
DB_PATH = os.path.join(ROOT, "viewer", "app.db")
KCACHE = os.path.join(ROOT, "predict_kcache.json")

# ---- 可调参数 -------------------------------------------------------------
CORE_N = 10                 # 核心池只数（多信号共振）
WATCH_N = 10                # 观察池只数（单信号强）
POOL_LIMIT = 160            # 候选池上限（= strategy_sim.universe 的 limit）
BARS = 660                  # 每只票取多少根日线（够「近两年 2×244」回放 + MA120 / 年线）
MIN_BARS = 130              # 少于此根数的票不参与技术面判定
WINDOWS = (1, 3, 5, 10)     # 复盘窗口（交易日）：1 日 / 3 日 / 5 日 / 10 日（2026-09-29 应王总加 T+1）
MAX_SERIES = 30             # 逐日曲线最多保留多少个交易日（历史沉淀）
PRIOR_HIT = 0.5             # 贝叶斯收缩先验命中率
PRIOR_N = 20                # 先验等效样本量
W_MIN, W_MAX = 0.02, 0.34   # 单信号权重上下限（防止一路信号独大；不再用 0.06 硬兜底）
ADJUST_LOG_EPS = 0.02       # 权重变动超过此值才记日志（防刷屏）
# 覆盖度校正（2026-10-01 新增）：命中票数少的信号说明它对候选池没代表性，
# 一律给等量权重等于把噪声当信号。故权重再乘一个「覆盖置信度」因子。
COVER_K = 30.0              # 覆盖置信度标度：命中约 30 只 → conf≈0.63；100 只 → conf≈0.96
COVER_FLOOR = 0.45          # 覆盖置信度下限保留比例（极低覆盖不至于被归零）
COVER_BLENDED = 0.45        # conf 对权重的混合起点（conf=0 时仍保留 45% 先验）
# ---- 主参考窗口（王总 2026-10-06 定）---------------------------------------
# 原话：「回测以近两年（一年半~两年）的涨跌为主要参考，太早的那些参考性不大。」
# 含义：所有**用于调参 / 算权重 / 覆盖度**的统计，只吃这个窗口内的样本；
# 窗口外的样本仍然留库可查（历史沉淀、逐日曲线不受影响），只是不再参与「自我进化」。
# 注意：这是**调参口径**，不是指标口径 —— 六路/十二路信号的定义一个字不改。
LOOKBACK_YEARS = 2.0        # 主参考窗口长度（年）
MIN_LOOKBACK_YEARS = 1.5    # 下限：一年半（王总口述区间）
LOOKBACK_DAYS = 10          # 情绪/资金/跟随类信号的回看自然日
SENT_POS = 0.15             # 情绪分 > 此值算看多
SENT_NEG = -0.15            # 情绪分 < 此值算看空

def window_start(today=None) -> str:
    """主参考窗口起点（YYYY-MM-DD）：今天回退 LOOKBACK_YEARS 年。

    只用于「哪些样本算数」的判定，不参与任何指标计算。取不到日期时返回空串
    （= 不过滤），宁可不算窗口，也不许把整批样本误判过期。
    """
    try:
        import datetime as _dt
        d = today or _dt.date.today()
        return (d - _dt.timedelta(days=int(round(LOOKBACK_YEARS * 365.0)))).isoformat()
    except Exception:  # noqa: BLE001 —— 日期算不出来就当作无窗口
        return ""


def window_caliber() -> dict:
    """回传给前端的口径说明：主参考窗口多长、从哪天起、当前样本落在窗口内的有几条。"""
    ws = window_start()
    return {"years": LOOKBACK_YEARS, "min_years": MIN_LOOKBACK_YEARS,
            "start": ws, "label": ("权重只按 %s 起的近 %.1f 年样本统计"
                                   % (ws, LOOKBACK_YEARS)) if ws else ""}


_LOCK = threading.Lock()    # 生成/复盘串行化（scheduler 与手动点击可能同时来）
_STATUS: dict = {"running": False, "stage": "", "started_at": None, "last_error": None}

# ---- 十二路信号元数据（顺序即前端展示顺序）--------------------------------
# prior = 先验权重系数（2026-10-01 按王总要求重构：覆盖度校正 + 维度扩到十二路）
#   取值来自三件事：① 数据在库里的实际覆盖率 ② 信号本身的"可信度"（是否可被证伪）
#   ③ 是否容易被刷分（游资上榜、外部信源都属易刷，故给低 prior）
SIGNALS = [
    {"key": "tech", "name": "技术面", "icon": "◈", "kind": "signal", "prior": 1.35,
     "desc": "86 条公开策略当日触发（按流派归并），池内命中策略数最多的优先。"},
    {"key": "holders", "name": "机构结构", "icon": "◎", "kind": "signal", "prior": 1.05,
     "desc": "前十大股东里机构（is_org）家数与持股变动、股东户数环比下降（机构结构趋优）。"},
    {"key": "ann", "name": "公告催化", "icon": "❐", "kind": "signal", "prior": 1.00,
     "desc": "近 10 日公告按类目分好坏：股权激励/回购增持/并购重组/产品突破 加分，"
             "减持/质押/退市风险/监管处罚 减分。"},
    {"key": "desk", "name": "盯盘结论", "icon": "⬢", "kind": "signal", "prior": 0.95,
     "desc": "盯盘模块当日结论：机会窗口加分、风险主导减分（含改判触发点）。"},
    {"key": "inst", "name": "机构席位", "icon": "◆", "kind": "signal", "prior": 0.85,
     "desc": "龙虎榜「机构专用」席位净买入（lhb_inst），按上榜原因分档加权；"
             "落点为 T+3~T+5（T+1 反向、涨占比 43.1%），故只作中期加分。"},
    {"key": "policy", "name": "政策指向", "icon": "⚑", "kind": "signal", "prior": 0.80,
     "desc": "中央级政策按 sectors 映射到个股，按重要度加权（内部资料）。"},
    {"key": "board", "name": "板块资金", "icon": "⁑", "kind": "signal", "prior": 0.70,
     "desc": "所属行业板块当日主力净额与涨跌幅（板块级映射，稀疏）。"},
    {"key": "sentiment", "name": "论坛情绪", "icon": "◉", "kind": "signal", "prior": 0.65,
     "desc": "近 10 日论坛提及热度 × 关联帖多空倾向（唯一领先型信号）。"},
    {"key": "overseas", "name": "海外联动", "icon": "⊕", "kind": "signal", "prior": 0.55,
     "desc": "地缘事件直接点名的个股 + 海外舆情传导板块（稀疏但指向明确）。"},
    {"key": "follow", "name": "选手跟随", "icon": "▣", "kind": "signal", "prior": 0.52,
     "desc": "选手**实时成交**（trades，非 2021 年快照 positions）按本人历史胜率加权。"},
    {"key": "extref", "name": "外部参考", "icon": "◇", "kind": "signal", "prior": 0.42,
     "desc": "外部信源（投顾/榜单/共识）看多信号，按信源层级加权；覆盖率极低故权重低。"},
    {"key": "lhb", "name": "游资上榜", "icon": "◈", "kind": "signal", "prior": 0.30,
     "desc": "龙虎榜上榜异动（lhb_stocks），按「上榜原因 + 席位属性」分档；"
             "实证 T+1 均值为负（涨幅类 +1.09%、换手 -1.10%、振幅 -2.39%、拉萨系 -2.20%），"
             "故仅作风险标记而非看多条。"},
]
SIGNAL_KEYS = [s["key"] for s in SIGNALS]
SIGNAL_META = {s["key"]: s for s in SIGNALS}
SIGNAL_PRIOR = {s["key"]: float(s.get("prior", 1.0)) for s in SIGNALS}

# ---- 龙虎榜校准表（L1 方法论校准层）---------------------------------------------
# 出处分两类：
#   A) 方法论 —— wb-finance-skill/references/ 下的方法论避坑条目；
#   B) 实证   —— 用库内 lhb_* 自带的 d1_chg/d5_chg 字段反推「上榜后真实股价反应」
#                （2026-10-01 实测，近 30 日样本；纯 SQL，无需 pandas）。
# 之所以要这张表：旧版 sig_lhb / sig_inst 不区分「因什么上榜」和「什么席位买的」，
# 把涨幅类、换手率类、振幅类、拉萨系一视同仁算成净买额 → 拿噪声当信号。
LHB_CALIB = {
    # 上榜触发原因 → 方向系数。实证 T+1 次日均值：涨幅类 +1.09% / 跌幅类 -0.46%
    # / 换手率类 -1.10% / 振幅类 -2.39%。故涨幅类可作正向，换手、振幅类应视为风险。
    "reason": {
        "up":       {"kw": ("涨幅", "涨幅偏离"), "f": 1.00,
                     "note": "涨幅类上榜 T+1 +1.09%"},
        "down":     {"kw": ("跌幅",),            "f": 0.25,
                     "note": "跌幅类上榜 T+1 -0.46%"},
        "turnover": {"kw": ("换手率", "换手达到"), "f": -0.60,
                     "note": "换手率上榜 T+1 -1.10%（高换手=分歧，非利好）"},
        "amplitude": {"kw": ("振幅",),           "f": -0.80,
                     "note": "振幅类上榜 T+1 -2.39%（最差）"},
    },
    # 席位属性 → 方向系数。实证（lhb_depts，近 30 日席位次日均值）：
    # 沪股通专用 +0.28% / 拉萨系(东财) -2.20% / 其它营业部 -0.23%。
    # 拉萨系印证了 leader-game.md「量化席位信号意义弱」，实盘比方法论更狠——是负的。
    "seat": {
        "north": {"kw": ("股通专用",), "f": 1.00,
                  "note": "沪股通/陆股通专用 席位次日 +0.28%"},
        "inst":  {"kw": ("机构专用",), "f": 0.80,
                  "note": "机构专用席位"},
        "lhasa": {"kw": ("拉萨", "东财", "东方财富"), "f": -0.60,
                  "note": "拉萨系/东财通道 席位次日 -2.20%"},
        "other": {"kw": (), "f": 0.00,
                  "note": "其它营业部 席位次日 -0.23%（不加不减）"},
    },
    # 机构专用席位（lhb_inst）的落点。实证 T+1 上涨占比仅 43.1%（<50%），
    # 且净买入>0 组 T+1 均值 -0.55% 而净买入<=0 组 +1.06% —— 短线是反向指标；
    # 但 T+3 +3.19% / T+5 +4.05% 是正向。本系统权重主轴是 T+5，故本路保留有效，
    # 但必须标注「落点是 T+3~T+5，不是 T+1」，避免拿次日噪声当利好。
    "inst_hold_days": (3, 5),
}
# 上榜次数极长尾（近 10 日有 9 次上榜的票）样本极小、且次日均值仍为负，
# 故同一票单次上榜只计一次强度，防止「刷榜」放大权重（实证：上榜1~3次 T+1 均为负）。
LHB_MAX_COUNT_PER_STOCK = 1

# ---- 表 ------------------------------------------------------------------
DDL = """
CREATE TABLE IF NOT EXISTS prediction_run (
  run_date     TEXT PRIMARY KEY,   -- 预测日（YYYY-MM-DD，即信号判定日）
  created_at   TEXT,
  engine_ver   TEXT,
  pool_n       INTEGER,            -- 候选池只数
  signal_n     INTEGER,            -- 有效信号数
  core_n       INTEGER,
  watch_n      INTEGER,
  market_view  TEXT,               -- 当日研判（一句话）
  style_view   TEXT,               -- 风格/主线判断
  breadth_json TEXT,               -- 各信号当日出票统计
  bench_json   TEXT,               -- 各窗口基准（候选池同期等权平均）
  status       TEXT,               -- ok | partial
  note         TEXT
);

CREATE TABLE IF NOT EXISTS prediction_item (
  run_date     TEXT NOT NULL,
  symbol       TEXT NOT NULL,
  name         TEXT,
  tier         TEXT,               -- core | watch
  rank         INTEGER,
  score        REAL,
  hit_signals  TEXT,               -- 命中信号明细 json
  reason       TEXT,
  entry_close  REAL,               -- T 日收盘（基准价）
  next_open    REAL,               -- T+1 开盘（供真实可成交性参考）
  ret_t1 REAL, ret_t3 REAL, ret_t5 REAL,
  bench_t1 REAL, bench_t3 REAL, bench_t5 REAL,
  hit_t1 INTEGER, hit_t3 INTEGER, hit_t5 INTEGER,
  caveat       TEXT,               -- 已到期的窗口全部为空 → '未到期'；有异常 → 说明
  reviewed_at  TEXT,
  PRIMARY KEY (run_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_pi_date ON prediction_item(run_date);
CREATE INDEX IF NOT EXISTS idx_pi_sym  ON prediction_item(symbol);

CREATE TABLE IF NOT EXISTS predictor_score (
  signal_key   TEXT PRIMARY KEY,
  kind         TEXT,               -- signal | school | strategy
  name         TEXT,
  school       TEXT,
  n_t1 INTEGER DEFAULT 0, n_hit_t1 INTEGER DEFAULT 0, sum_ret_t1 REAL DEFAULT 0,
  n_t3 INTEGER DEFAULT 0, n_hit_t3 INTEGER DEFAULT 0, sum_ret_t3 REAL DEFAULT 0,
  n_t5 INTEGER DEFAULT 0, n_hit_t5 INTEGER DEFAULT 0, sum_ret_t5 REAL DEFAULT 0,
  weight       REAL,
  weight_prev  REAL,
  updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS predictor_adjust_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  log_date      TEXT,
  signal_key    TEXT,
  kind          TEXT,
  weight_before REAL,
  weight_after  REAL,
  reason        TEXT,
  created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pal_date ON predictor_adjust_log(log_date);

-- 近两年逐日回放（王总 2026-10-06 定：主参考窗口＝近两年）
CREATE TABLE IF NOT EXISTS predictor_replay (
  id           INTEGER PRIMARY KEY,
  window_label TEXT,            -- y2（近两年）… 与 strategy_sim_window 口径对齐
  days         INTEGER,         -- 回放交易日数
  pool         INTEGER,         -- 候选池只数
  start_date   TEXT,
  end_date     TEXT,
  scores_json  TEXT,            -- {key:{t3:{n,hit_rate,avg_ret}, t5:…, t10:…}}
  computed_at  TEXT
);
"""

_SCORE_COLS = ("kind", "name", "school",
               "n_t1", "n_hit_t1", "sum_ret_t1",
               "n_t3", "n_hit_t3", "sum_ret_t3",
               "n_t5", "n_hit_t5", "sum_ret_t5")


# ---------------------------------------------------------------- 基础设施
def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=30000")
    except Exception:  # noqa: BLE001
        pass
    return con


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()
    _migrate(con)
    _drop_stale_signal_keys(con)
    # 新维度 / 新信号的记账行也要自愈：只删不补会让 /api/prediction/scores 只返回旧那几路
    # （2026-10-01 实测：删完 fund 后接口只剩 5 路，新 12 路的行要等下一次 generate 才出现）。
    try:
        _ensure_score_rows(con, [])
    except Exception:  # noqa: BLE001
        pass


def _migrate(con: sqlite3.Connection) -> None:
    """幂等加列迁移（SQLite 不支持 IF NOT EXISTS 的 ADD COLUMN）。"""
    cols_item = {
        "ret_t10": "REAL", "bench_t10": "REAL", "hit_t10": "INTEGER",
        "series_json": "TEXT",
    }
    for col in (1, 3, 5, 10):
        pass  # 窗口列已随 DDL（t3/t5 旧，t10 在下面补）
    existing = {r["name"] for r in con.execute("PRAGMA table_info(prediction_item)").fetchall()}
    for col, typ in cols_item.items():
        if col not in existing:
            try:
                con.execute("ALTER TABLE prediction_item ADD COLUMN %s %s" % (col, typ))
            except Exception:  # noqa: BLE001
                pass
    existing = {r["name"] for r in con.execute("PRAGMA table_info(predictor_score)").fetchall()}
    for col in ("n_t10", "n_hit_t10", "sum_ret_t10"):
        if col not in existing:
            try:
                con.execute("ALTER TABLE predictor_score ADD COLUMN %s REAL" % col)
            except Exception:  # noqa: BLE001
                pass
    con.commit()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _latest_trade_date_anchor() -> str:
    """预测 / K 线刷新锚点：动态计算「真实最近【已收盘】交易日」，绝不依赖缓存旧日期或裸 today()。

    硬保证（用户原话：每天预测和抓取的数据都该是实时和当天的，拿当天去预测，
    不是昨天的、也不是上个月的）：
      - 收盘后（>=15:00）：锚点 = auction.latest_trade_date()（含今日，若今日是交易日且已收盘）。
      - 盘中 / 盘前（<15:00）：锚点 = auction.latest_trade_date(昨日)（严格早于今日，
        避免用半根 K 线预测今日 —— 今日这根 K 线收盘前会变）。
      - 取不到时退回 _today()（极端兜底，绝不用缓存里的旧日期）。
    auction.latest_trade_date 已叠加 2026 确定性交易日历兜底（国庆 10/01–10/07 等），
    即使 Sina K 线源未刷新也能正确识别刚过去的交易日。
    """
    from auction import latest_trade_date as _au_ltd
    now = datetime.now()
    if now.hour >= 15:
        td = _au_ltd()
    else:
        y = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        td = _au_ltd(y) or _au_ltd()
    return td or _today()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _f(x, d=None):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return d
        return v
    except (TypeError, ValueError):
        return d


def _pct_le(sorted_vals: list, x) -> float:
    """x 在已排序列表 sorted_vals 中的百分位（0~1）。用于把不同量纲的强度归一到可比区间。"""
    if not sorted_vals:
        return 0.5
    lo, hi = 0, len(sorted_vals)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_vals[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo / len(sorted_vals)


def status_snapshot() -> dict:
    return dict(_STATUS)


# ---------------------------------------------------------------- K 线缓存
def load_kcache() -> dict:
    if os.path.exists(KCACHE):
        try:
            with open(KCACHE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "bars" in d:
                return d
        except Exception:  # noqa: BLE001
            pass
    return {"updated": None, "bars": {}}


def save_kcache(d: dict) -> None:
    try:
        tmp = KCACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, KCACHE)
    except Exception as e:  # noqa: BLE001
        print("[predictor] 缓存写入失败:", str(e)[:100], flush=True)


def fetch_bars(codes: list, kc: dict, force: bool = False,
               progress=None) -> dict:
    """按需抓取 K 线：缓存里最后一根日期 >= 今天的（或最近交易日）就不再抓。

    返回 {code: [bar, ...]}；bar = {d,o,h,l,c,v}。
    """
    bars = kc.setdefault("bars", {})
    today = _today()
    out, need = {}, []
    for c in codes:
        v = bars.get(c) or []
        if not force and v and str(v[-1].get("d", ""))[:10] >= today:
            out[c] = v
        elif (not force and v and kc.get("updated") == today
              and datetime.now().hour < 15):
            # ⚠️ "今天抓过就跳过"只允许发生在**盘中**（hour<15）。
            # 旧版无时段限制：盘中任何一次触发（重启/手动/预览）抓到的日线只有到
            # 昨日的完整 bar，却把 updated 标成今天 → 15:05 收盘任务复用盘中旧
            # K 线，交易日众数停在昨日，当日预测永不生成（2026-09-29/30 连续两天复现）。
            # 收盘后（>=15 点）必须看到 v[-1].d >= today（第一条短路）才算新鲜，否则重抓。
            out[c] = v
        else:
            need.append(c)
    if need:
        lock = threading.Lock()
        done = [0]
        ok_cnt = [0]

        def one(code):
            try:
                v = SS.fetch_kline(code, BARS)
            except Exception:  # noqa: BLE001
                v = []
            with lock:
                done[0] += 1
                if v:
                    ok_cnt[0] += 1
                if progress and done[0] % 20 == 0:
                    progress(done[0], len(need))
            return code, v

        with __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=8) as ex:
            for code, v in ex.map(one, need):
                if v:
                    bars[code] = v
                    out[code] = v
        # ⚠️ 只有"确实抓到了数据"才把 updated 标成今天。
        # 旧版无条件写 updated=today → 全部抓失败的那天（如新浪限流）会被当成
        # "今天已经抓过了"，此后每次都复用旧 K 线，预测日永远停在旧交易日。
        if ok_cnt[0] >= max(1, len(need) // 2):
            kc["updated"] = today
            save_kcache(kc)
        else:
            print("[predictor] fetch_bars: 本轮抓取成功率过低（%d/%d），不更新缓存标记，下次重试"
                  % (ok_cnt[0], len(need)), flush=True)
    return out


def _trade_date_of(bars_map: dict) -> str | None:
    """候选池 K 线的最后一根日期（取众数，避免个别停牌票拖后腿）。"""
    cnt = Counter()
    for v in bars_map.values():
        if v:
            cnt[str(v[-1].get("d", ""))[:10]] += 1
    return cnt.most_common(1)[0][0] if cnt else None


# ---------------------------------------------------------------- 信号：技术面
# ---- 策略门控（王总 2026-10-06 定：按「近两年」回测取舍技术面策略）------------------
# 口径说明：六路 / 十二路信号的定义一个字不改，只改「哪些策略能进技术面信号」。
# 判定依据全部来自 strategy_sim_window 表的主参考窗口（STRAT_GATE_WINDOW = 近两年回测结果）。
STRAT_GATE_ENABLED = True
STRAT_GATE_WINDOW = "mid"          # 与 strategy_sim.MAIN_WINDOW_LABEL 对齐
STRAT_GATE_MIN_TRADES = 30         # 近两年交易次数下限：低于此视为样本不足 → 剔除
STRAT_GATE_MIN_TOTAL_RET = 0.0     # 近两年总收益下限（百分数）：为负即近两年跑不动 → 剔除
STRAT_GATE_DROP_RATING = "较差"    # 近两年评级达到该档及以下的 → 剔除
STRAT_GATE_STATE = {"kept": 0, "dropped": 0, "drop": []}   # 供日志 / 接口读取


def _sim_window_rows() -> dict:
    """读 strategy_sim_window 主参考窗口的各策略回测结果 → {strategy_key: Row}。

    读不到（无回测记录 / 表不存在 / 窗口为空）时返回空 dict，调用方按「全量策略」降级运行，
    绝不因为回测数据缺失而误杀策略。
    """
    if not STRAT_GATE_ENABLED:
        return {}
    try:
        if not os.path.exists(DB_PATH):
            return {}
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM strategy_sim_window WHERE window_label=?",
                           (STRAT_GATE_WINDOW,)).fetchall()
        con.close()
        return {r["strategy_key"]: r for r in rows}
    except Exception as exc:  # noqa: BLE001
        print("[predictor] 读策略回测窗口失败，本轮按全量策略运行：%s" % exc, flush=True)
        return {}


def _gate_strategies(strat_list: list) -> list:
    """按近两年回测结果过滤策略：跑不动 / 样本不足 / 评级差的，从技术面信号里剔掉。"""
    sim = _sim_window_rows()
    STRAT_GATE_STATE.update({"kept": 0, "dropped": 0, "drop": []})
    if not sim:
        print("[predictor] 近两年回测窗口为空，本轮不放门控（全量策略）", flush=True)
        return strat_list
    keep, dropped = [], []
    for s in strat_list:
        r = sim.get(s.get("key"))
        if r is None:
            keep.append(s)          # 没跑过回测的不擅自剔除
            continue
        why = []
        tr, ret = r["trades"], r["total_return"]
        rating = (r["rating"] or "")
        if tr is not None and tr < STRAT_GATE_MIN_TRADES:
            why.append("近两年仅 %s 次交易(< %d)" % (tr, STRAT_GATE_MIN_TRADES))
        if ret is not None and ret < STRAT_GATE_MIN_TOTAL_RET:
            why.append("近两年总收益 %.2f%%(<0)" % ret)
        if rating == STRAT_GATE_DROP_RATING:
            why.append("近两年评级「较差」")
        if why:
            dropped.append((s.get("key"), "; ".join(why)))
        else:
            keep.append(s)
    STRAT_GATE_STATE["kept"] = len(keep)
    STRAT_GATE_STATE["dropped"] = len(dropped)
    STRAT_GATE_STATE["drop"] = dropped
    if dropped:
        print("[predictor] 近两年窗口策略门控：保留 %d 条，剔除 %d 条" %
              (len(keep), len(dropped)), flush=True)
        for k, w in dropped:
            print("   - %-26s %s" % (k, w), flush=True)
    return keep


def _load_strategies() -> list:
    out = []
    for s in STRATEGIES or []:
        if not s.get("backtestable"):
            continue
        if s.get("scope_limited"):
            continue
        if s.get("needs"):            # 需要指数/宽度等额外上下文的，本轮跳过（择时类）
            continue
        out.append(s)
    return _gate_strategies(out)


def sig_tech(bars_map: dict, strat_list: list, progress=None) -> dict:
    """技术面：每票跑全部策略，取「最后一根 K 线触发买入」的。

    返回 {code: {"strength": hit_n, "schools": [...], "strategies": [key...]}}
    """
    res: dict = {}
    n = len(bars_map)
    for idx, (code, k) in enumerate(bars_map.items()):
        if len(k) < MIN_BARS:
            continue
        hits, schools = [], set()
        for s in strat_list:
            key = s.get("key")
            try:
                buy, _sell = SS.build_signals(key, k, code=code, name="")
            except Exception:  # noqa: BLE001
                continue
            if buy and buy[-1]:
                hits.append(key)
                schools.add(s.get("school") or "其它")
        if hits:
            res[code] = {"strength": len(hits), "strategies": hits,
                         "schools": sorted(schools)}
        if progress and (idx + 1) % 20 == 0:
            progress(idx + 1, n)
    return res


def sig_tech_replay(k: list, strat_list: list) -> dict:
    """历史回放用：一次算完整序列，返回 {strategy_key: [bool,...]}（长度 = len(k)）。

    避免按日重算 —— build_signals 本就返回整段序列，直接复用。
    """
    out: dict = {}
    for s in strat_list:
        key = s.get("key")
        try:
            buy, _sell = SS.build_signals(key, k)
        except Exception:  # noqa: BLE001
            continue
        out[key] = buy
    return out


# ---------------------------------------------------------------- 信号：其它五路
def sig_sentiment(con: sqlite3.Connection) -> dict:
    """论坛情绪：近 LOOKBACK_DAYS 天，某票被提及次数 + 关联帖平均情绪分。

    strength = 提及次数 × (1 + 情绪分)，情绪为负则打折。
    """
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    rows = con.execute(
        "SELECT m.symbol, m.symbol_name, p.sentiment, p.sentiment_score "
        "FROM stock_mentions m LEFT JOIN posts p ON p.id = m.post_id_ref "
        "WHERE p.post_time >= ? AND m.symbol IS NOT NULL AND m.symbol != ''",
        (since,)).fetchall()
    agg: dict = {}
    for r in rows:
        sym = str(r["symbol"]).strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        a = agg.setdefault(sym, {"n": 0, "sum": 0.0, "cnt": 0, "name": None})
        a["n"] += 1
        a["name"] = a["name"] or (r["symbol_name"] or None)
        sc = _f(r["sentiment_score"])
        if sc is not None:
            a["sum"] += sc
            a["cnt"] += 1
    out: dict = {}
    for sym, a in agg.items():
        avg = (a["sum"] / a["cnt"]) if a["cnt"] else 0.0
        # sentiment_score 实测约 -14~3，直接按该口径归一（不再假设 0~100）
        tone = 1.0 + max(-1.0, min(1.0, avg / 4.0))
        strength = a["n"] * tone
        if strength <= 0:
            continue
        ev = ["近%d日提及 %d 次" % (LOOKBACK_DAYS, a["n"])]
        if a["cnt"]:
            ev.append("情绪%+.2f" % avg)
        out[sym] = {"strength": strength, "evidence": ev, "name": a["name"]}
    return out


def _lhb_reason_tier(reason: str) -> tuple[str, float]:
    """龙虎榜「上榜原因」→ (档位 key, 方向系数)。查不到原因时按中性 0.3。

    依据见 LHB_CALIB["reason"]（库内 d1_chg 后验 + leader-game.md）。
    """
    r = str(reason or "")
    for k, v in LHB_CALIB["reason"].items():
        for kw in v["kw"]:
            if kw in r:
                return k, v["f"]
    return "unknown", 0.30


def _lhb_seat_factor(names: str) -> tuple[str, float]:
    """席位名 → (档位 key, 方向系数)。names 可含多个逗号分隔席位（lhb_depts.org_name）。

    依据见 LHB_CALIB["seat"]。拉萨系/东财通道实证次日均值 -2.20%，是明确负向。
    """
    n = str(names or "")
    for k, v in LHB_CALIB["seat"].items():
        if not v["kw"]:
            continue
        for kw in v["kw"]:
            if kw in n:
                return k, v["f"]
    return "other", LHB_CALIB["seat"]["other"]["f"]


def sig_inst(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """龙虎榜「机构专用」席位（lhb_inst）：近 LOOKBACK_DAYS 天机构净买入。

    为什么单独成一路：2026-10-01 王总指出「龙虎榜很多查不到资金、且很多是模拟的、参考性不强」。
    实测口径 —— 近 10 日 lhb_stocks 有 487 行、net_amt 缺失仅 0.4%，所以"查不到"不是数据缺失，
    而是 `if net <= 0: continue` 把净卖出的全丢了；而"参考性不强"是可以量化的：
    近 10 日 lhb_depts 有 3123 条营业部席位行，拉萨系/方新侠/章盟主这类席位高度同质，
    同一批游资反复上榜，"上榜次数"基本等于噪声。
    但龙虎榜里有一块刷不了 —— **机构专用席位**（lhb_inst，近 10 日 259 行、池内覆盖 64 票），
    它必须真金白银下单。故拆成两路：本函数（机构，硬钱）+ sig_lhb（游资，噪声级）。

    asof：数据隔离边界，只统计 trade_date <= asof。
    """
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    if asof:
        try:
            since = (datetime.strptime(asof, "%Y-%m-%d")
                     - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        except ValueError:
            asof = None
    sql = ("SELECT security_code, security_name, trade_date, net_buy_amt, ratio, buy_times, "
           "reason FROM lhb_inst WHERE trade_date >= ?")
    args = [since]
    if asof:
        sql += " AND trade_date <= ?"
        args.append(asof)
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    agg: dict = {}
    for r in rows:
        sym = str(r["security_code"] or "").strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        net = _f(r["net_buy_amt"], 0.0) or 0.0
        if net <= 0:
            continue
        ratio = _f(r["ratio"], 0.0) or 0.0
        a = agg.setdefault(sym, {"net": 0.0, "n": 0, "name": None, "ratio": 0.0,
                                 "graded": {}, "raw": 0.0})
        tier, f = _lhb_reason_tier(r["reason"])
        a["graded"][tier] = a["graded"].get(tier, 0.0) + net
        a["raw"] += net
        a["net"] += net
        a["n"] += 1
        a["ratio"] = max(a["ratio"], abs(ratio))
        a["name"] = a["name"] or (r["security_name"] or None)
    out: dict = {}
    for sym, a in agg.items():
        # 按上榜原因分档加权：涨幅类可作正向，换手/振幅类是风险（实证 T+1 为负）。
        # leader-game.md：「假机构席位容易误导」「量化席位信号意义弱」——
        # 但机构专用是真硬钱，故只做口径校正而非整体否决。
        w_net = 0.0
        for tier, net_t in a["graded"].items():
            f = LHB_CALIB["reason"].get(tier, {}).get("f", 0.30)
            w_net += (net_t / 1e8) * f
        # ratio（机构买入占该股成交比）越大越硬 —— 机构真金砸盘 vs 顺手买一点，含金量差很多
        strength = w_net * (1.0 + min(a["ratio"], 60.0) / 100.0)
        good = [k for k in a["graded"] if LHB_CALIB["reason"].get(k, {}).get("f", 0) > 0]
        ev = ["近%d日机构专用席位%d次" % (LOOKBACK_DAYS, a["n"]),
              "原始净买 %+.2f 亿" % (a["raw"] / 1e8),
              "加权后 %+.2f 亿" % w_net,
              "占成交 %.1f%%" % a["ratio"],
                                 "榜单类型：" + ("/".join(good) if good else "无正向类型"),
                                 "落点 T+3~T+5（T+1 涨占比仅 43.1%、净买>0 组 T+1 -0.55% "
                                 "而净买<=0 组 +1.06%，短线为反向）"]
        out[sym] = {"strength": strength, "evidence": ev, "name": a["name"]}
    return out


def sig_lhb(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """龙虎榜营业部 / 游资席位：近 LOOKBACK_DAYS 天上榜净买入（lhb_stocks 口径）。

    权重刻意压到噪声级（prior=0.30）：席位同质化、同一批游资反复上榜、"上榜次数"不是有效强度。
    只保留给"异动识别"用，不承担主要打分。
    """
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    if asof:
        try:
            since = (datetime.strptime(asof, "%Y-%m-%d")
                     - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        except ValueError:
            asof = None
    sql = ("SELECT security_code, security_name, trade_date, net_amt, reason "
           "FROM lhb_stocks WHERE trade_date >= ?")
    args = [since]
    if asof:
        sql += " AND trade_date <= ?"
        args.append(asof)
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    # 席位属性：lhb_stocks 只有 buy_seat/sell_seat（数字），席位名在 lhb_depts。
    # 取每票最近一次上榜时的席位明细做分层。
    seat_map: dict = {}
    try:
        seat_sql = (
            "SELECT d.security_code, d.org_name FROM lhb_depts d JOIN ("
            "  SELECT security_code, MAX(trade_date) md FROM lhb_depts "
            "  WHERE trade_date >= ? GROUP BY security_code"
            ") t ON d.security_code = t.security_code AND d.trade_date = t.md"
        )
        seat_args = [since]
        if asof:
            seat_sql += " AND d.trade_date <= ?"
            seat_args.append(asof)
        for r in con.execute(seat_sql, seat_args):
            seat_map[str(r[0] or "").strip()] = str(r[1] or "")
    except Exception:  # noqa: BLE001
        seat_map = {}

    agg: dict = {}
    for r in rows:
        sym = str(r["security_code"] or "").strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        net = _f(r["net_amt"], 0.0) or 0.0
        if net <= 0:
            continue
        tier, f_reason = _lhb_reason_tier(r["reason"])
        seat_key, f_seat = _lhb_seat_factor(seat_map.get(sym, ""))
        # 单次上榜只计一次强度（取该票各次里加权后最高的那次）：
        # 实证近 10 日同票上榜 2~9 次的次日均值几乎全为负（1次 -1.72% … 7次 -3.56%），
        # 所谓"上榜次数"基本是噪声，按次数累加等于给刷榜票加权。
        a = agg.setdefault(sym, {"n": 0, "name": None, "raw": 0.0, "best": 0.0,
                                 "tier": tier, "seat": seat_key})
        val = (net / 1e8) * f_reason * (1.0 + f_seat)
        if val > a["best"]:
            a["best"] = val
            a["tier"], a["seat"] = tier, seat_key
        a["raw"] += net
        a["n"] += 1
        a["name"] = a["name"] or (r["security_name"] or None)
    out: dict = {}
    for sym, a in agg.items():
        out[sym] = {"strength": a["best"],
                    "evidence": ["近%d日上榜%d次（取加权最高的一次）" % (LOOKBACK_DAYS, a["n"]),
                                 "原始净买 %+.2f 亿" % (a["raw"] / 1e8),
                                 "榜单类型：" + a["tier"] + "（" +
                                 LHB_CALIB["reason"].get(a["tier"], {}).get("note", "未知"),
                                 "席位：" + a["seat"] + "（" +
                                 LHB_CALIB["seat"].get(a["seat"], {}).get("note", "未知"),
                                 "→ 该档 T+1 实证为负，仅作异动/风险标记，不作看多依据"],
                    "name": a["name"]}
    return out


def _industry_index(con: sqlite3.Connection) -> dict:
    """板块词 → 个股集合。stock_org.industry 是「一级-二级-三级」三段式，
    拆开建反查，政策/海外/板块资金的板块词才能落到个股上。"""
    idx: dict = {}
    try:
        for r in con.execute(
                "SELECT code, industry FROM stock_org "
                "WHERE industry IS NOT NULL AND industry != ''"):
            code = str(r["code"] or "").strip().zfill(6)
            if not code.isdigit():
                continue
            for part in str(r["industry"]).split("-"):
                part = part.strip()
                if part:
                    idx.setdefault(part, set()).add(code)
    except Exception:  # noqa: BLE001
        pass
    return idx


def _expand_sector_words(idx: dict, words: list) -> set:
    """板块词 → 个股（双向包含匹配：'传媒' 命中 '文化传媒-影视动漫-影视'，反之亦然）。"""
    hit: set = set()
    for w in words:
        w = str(w).strip()
        if not w:
            continue
        base = w.split("(")[0].rstrip("、").strip()
        for part, codes in idx.items():
            if base in part or part in base:
                hit |= codes
    return hit


def sig_ann(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """公告催化：近 N 日公告按类目分好坏（stock_ann 已有 cat/tone 分类）。

    这是最硬的一类信号 —— 公告是白纸黑字、可证伪，不像情绪和席位那样可刷。
    """
    GOOD = ("股权激励", "回购增持", "并购重组", "产品/资质突破", "股东回报", "业绩预增")
    BAD = ("股东减持", "质押担保", "退市风险", "监管处罚", "减值计提", "业绩预降", "诉讼仲裁")
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS * 2)).strftime("%Y-%m-%d")
    sql = "SELECT code, name, notice_date, cat, tone FROM stock_ann WHERE notice_date >= ?"
    args = [since]
    if asof:
        sql += " AND notice_date <= ?"
        args.append(asof)
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    agg: dict = {}
    for r in rows:
        sym = str(r["code"] or "").strip().zfill(6)
        if len(sym) != 6 or not sym.isdigit():
            continue
        cat = str(r["cat"] or "")
        # 2026-10-01 口径自查：判据改用入库时已定好的 tone（分类规则唯一真源 = newsclass/watchdesk）。
        # 改前这里按「类目名子串 + 两张硬编码白名单」猜好坏，是同一批标签的第三份副本，
        # 命名与上游不同步（"业绩预增" vs "业绩向好"、"回购增持" vs "股东回报"），改个词就静默失效。
        # tone 缺失的老数据回落旧匹配，避免历史信号整体归零。
        tone = str(r["tone"] or "")
        a = agg.setdefault(sym, {"good": 0, "bad": 0, "name": None, "cats": []})
        if tone == "good" or (not tone and any(g in cat for g in GOOD)):
            a["good"] += 1
            a["cats"].append(cat)
        elif tone == "bad" or (not tone and any(b in cat for b in BAD)):
            a["bad"] += 1
            a["cats"].append(cat)
        a["name"] = a["name"] or (r["name"] or None)
    out: dict = {}
    for sym, a in agg.items():
        # 坏消息只扣 0.6 分：短线里"坏消息落地"有时反而是买点，不宜全额抵消
        strength = a["good"] - 0.6 * a["bad"]
        if strength <= 0:
            continue
        ev = ["近%d日利好公告 %d 条" % (LOOKBACK_DAYS * 2, a["good"])]
        if a["bad"]:
            ev.append("利空 %d 条" % a["bad"])
        out[sym] = {"strength": strength, "evidence": ev, "name": a["name"]}
    return out


def sig_desk(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """盯盘结论：watchdesk 模块当日产出（stock_desk：stance / risk_score / opp_score）。

    双向信号 —— 风险主导（risk_score 高）应当负贡献。盯盘结论是内部研判产物，
    与政策、公告同属"内部资料"一路。

    隔离口径说明：stock_desk 是 watchdesk 每次扫描后**全量重写**的快照表（没有历史版本），
    和 posts/positions 一样无法按日期回溯。若按 asof 过滤 `date(scan_at) <= asof`，
    回补历史日预测时这路信号恒为 0 —— 等于用技术性的日期条件把它静默杀掉。
    这里按"快照语义"处理：照常参与，如实标注，不假装隔离。
    """
    sql = "SELECT code, name, stance, risk_score, opp_score, scan_at FROM stock_desk"
    args: list = []
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for r in rows:
        sym = str(r["code"] or "").strip().zfill(6)
        if len(sym) != 6 or not sym.isdigit():
            continue
        opp = _f(r["opp_score"], 0.0) or 0.0
        risk = _f(r["risk_score"], 0.0) or 0.0
        strength = opp - risk
        if strength <= 0:
            continue
        st = str(r["stance"] or "")
        ev = ["盯盘：%s" % st]
        if opp:
            ev.append("机会分 %g" % opp)
        if risk:
            ev.append("风险分 %g" % risk)
        out[sym] = {"strength": strength, "evidence": ev, "name": r["name"] or None}
    return out


def sig_policy(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """政策指向（内部资料）：中央级政策按 sectors 映射到个股，按 importance 加权。

    实测：policy_news 共 73 条，其中不少 sectors 为空；能落到候选池的板块词约占 22%
    （172 个板块词里 38 个命中池内票）。属天然稀疏信号 —— 靠 _cover_conf() 自动压权重，
    命中即加分、不命中不加分，不会稀释大盘。
    """
    out: dict = {}
    idx = _industry_index(con)
    sql2 = "SELECT key, sectors, importance, level, title, pub_time FROM policy_news"
    args2: list = []
    if asof:
        sql2 += " WHERE (pub_time IS NULL OR pub_time = '' OR pub_time <= ?)"
        args2.append(asof)
    try:
        rows = con.execute(sql2 + " ORDER BY id", args2).fetchall()
    except Exception:  # noqa: BLE001
        return out
    agg: dict = {}
    for r in rows:
        try:
            words = json.loads(r["sectors"] or "[]")
        except Exception:  # noqa: BLE001
            words = []
        if not isinstance(words, list) or not words:
            continue
        hit = _expand_sector_words(idx, [str(w) for w in words])
        if not hit:
            continue
        imp = _f(r["importance"], 1.0) or 1.0
        lvl = 1.5 if str(r["level"] or "") == "中央" else 1.0
        wgt = imp * lvl
        for code in hit:
            a = agg.setdefault(code, {"s": 0.0, "n": 0, "title": None, "name": None})
            a["s"] += wgt
            a["n"] += 1
            a["title"] = a["title"] or (str(r["title"] or "")[:24] or None)
    for code, a in agg.items():
        out[code] = {"strength": a["s"],
                     "evidence": ["政策命中 %d 条" % a["n"]] + ([a["title"]] if a["title"] else []),
                     "name": a["name"]}
    return out


def sig_board(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """板块资金：所属行业板块当日主力净额（board_flow_samples，kind='industry'）。"""
    idx = _industry_index(con)
    sql = ("SELECT board_name, trade_date, main_net, pct FROM board_flow_samples "
           "WHERE kind='industry'")
    args: list = []
    if asof:
        sql += " AND trade_date <= ?"
        args.append(asof)
    try:
        # 注意：board_flow_samples **没有 id 列**（建表时未建），ORDER BY id 会直接抛
        # OperationalError，异常被子句吞掉后整路信号变空 —— 2026-10-01 实测踩过。
        # 这里按 trade_date/ts 排序取最近交易日，并放宽到最近 3 天聚合，避免单日板块缺失。
        rows = con.execute(sql + " ORDER BY trade_date DESC, ts DESC", args).fetchall()
    except Exception as e:  # noqa: BLE001
        return {}
    if not rows:
        return {}
    last_day = str(rows[0]["trade_date"] or "")
    # 先只取最近一个交易日（同一板块跨日相加会让净额虚高出「1189 亿」这种离谱值）；
    # 若该日一条都没匹配上，再放宽到最近 3 天。
    out: dict = {}
    for days_back in (0, 2):
        cut = last_day
        if days_back:
            try:
                cut = (datetime.strptime(last_day, "%Y-%m-%d")
                       - timedelta(days=3)).strftime("%Y-%m-%d")
            except ValueError:
                cut = last_day
        agg: dict = {}
        for r in rows:
            d = str(r["trade_date"] or "")
            if d != last_day and (not cut or d < cut):
                continue
            codes = _expand_sector_words(idx, [str(r["board_name"] or "")])
            if not codes:
                continue
            net = _f(r["main_net"], 0.0) or 0.0
            if net <= 0:
                continue
            pct = _f(r["pct"], 0.0) or 0.0
            for code in codes:
                a = agg.setdefault(code, {"net": 0.0, "pct": 0.0})
                a["net"] += net
                a["pct"] = max(a["pct"], pct)
        if agg:
            # main_net 原始单位不明（实测换算成"亿"会得出千亿级离谱值），
            # 故只给相对强度，不标绝对金额单位，避免误导。
            for code, a in agg.items():
                out[code] = {"strength": a["net"],
                             "evidence": ["所属板块资金强度 %.2f" % a["net"],
                                          "板块涨 %.2f%%" % a["pct"]],
                             "name": None}
            break
    return out


def sig_overseas(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """海外联动：地缘事件直点个股（geo_events.symbols_up，最准）+ 海外舆情传导板块。

    实测 geo_events 63 只直点符号里只有 1 只在池内 —— 天然稀疏，命中即是有价值信号。
    """
    idx = _industry_index(con)
    out: dict = {}
    try:
        rows = con.execute("SELECT symbols_up, symbols_down, sectors_up, sectors_down, "
                           "intensity, direction, event_date, title FROM geo_events")
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        if asof:
            ed = str(r["event_date"] or "")[:10]
            if ed and ed > asof:
                continue
        try:
            ups = _json_loads(r["symbols_up"])
            downs = _json_loads(r["symbols_down"])
        except Exception:  # noqa: BLE001
            ups, downs = [], []
        codes = set()
        for it in ups:
            if isinstance(it, dict) and it.get("code"):
                codes.add(str(it["code"]).strip().zfill(6))
        for it in downs:
            if isinstance(it, dict) and it.get("code"):
                codes.add(str(it["code"]).strip().zfill(6))
        sectors = [str(x) for x in _json_loads(r["sectors_up"])]
        sectors = [s for s in sectors if s]
        if not codes and sectors:
            codes = _expand_sector_words(idx, sectors)
        if not codes:
            continue
        inten = _f(r["intensity"], 2.0) or 2.0
        for code in codes:
            a = out.setdefault(code, {"s": 0.0, "n": 0, "title": None})
            a["s"] += min(inten, 5.0) / 2.0
            a["n"] += 1
            a["title"] = a["title"] or (str(r["title"] or "")[:24] or None)
    # 海外舆情传导（overseas_hotspots.domestic_sectors）
    try:
        for r in con.execute("SELECT domestic_sectors, heat, confidence, title FROM overseas_hotspots"):
            sectors = [x.strip() for x in str(r["domestic_sectors"] or "").split(",") if x.strip()]
            codes = _expand_sector_words(idx, sectors)
            if not codes:
                continue
            heat = _f(r["heat"], 0.0) or 0.0
            conf = _f(r["confidence"], 0.6) or 0.6
            w = (1.0 + min(heat, 500.0) / 200.0) * (0.5 + conf)
            title = str(r["title"] or "")[:24]
            for code in codes:
                a = out.setdefault(code, {"s": 0.0, "n": 0, "title": None})
                a["s"] += w
                a["n"] += 1
                a["title"] = a["title"] or title or None
    except Exception:  # noqa: BLE001
        pass
    # overseas_news.sectors（部分行有板块）
    try:
        for r in con.execute("SELECT sectors, sentiment_score, heat, title FROM overseas_news"):
            raw = str(r["sectors"] or "")
            if not raw or raw == "[]":
                continue
            try:
                words = json.loads(raw)
            except Exception:  # noqa: BLE001
                words = []
            codes = _expand_sector_words(idx, [str(w) for w in words]) if isinstance(words, list) else set()
            if not codes:
                continue
            sent = _f(r["sentiment_score"], 0.0) or 0.0
            if sent < 0:
                continue
            heat = _f(r["heat"], 0.0) or 0.0
            w = (1.0 + min(heat, 100.0) / 60.0) * (0.7 + min(sent, 3.0) / 6.0)
            title = str(r["title"] or "")[:24]
            for code in codes:
                a = out.setdefault(code, {"s": 0.0, "n": 0, "title": None})
                a["s"] += w
                a["n"] += 1
                a["title"] = a["title"] or title or None
    except Exception:  # noqa: BLE001
        pass
    for code, a in out.items():
        out[code] = {"strength": a["s"],
                     "evidence": ["海外/地缘 %d 条指向" % a["n"]] + ([a["title"]] if a["title"] else []),
                     "name": None}
    return out


def _json_loads(x) -> list:
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return list(x)
    try:
        v = json.loads(x)
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def sig_follow(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """实盘选手跟随：改用 **trades 实时成交**，按选手本人的历史胜率加权。

    2026-10-01 重要修正：原实现读 positions 表，而 positions 最新 post_time = 2021-06-20
    —— 也就是说界面上那批"选手跟随"其实是五年前的快照，根本不是跟随。
    trades 表有当天（如 2026-10-01 12:31）的真实成交，且带 action（BUY/SELL/HOLD），
    才是活的信号。权重按 person_scores 的真实胜率给，不再用拍脑袋的 influence/tier_weight。

    asof：数据隔离边界（按 post_time 字符串比较，格式 YYYY-MM-DD HH:MM:SS）。
    """
    # 1) 选手历史胜率 → 0~1（无胜率记录者取中性 0.5）
    win: dict = {}
    try:
        for r in con.execute(
                "SELECT name, win_rate_5d, win_rate_20d, best_call_ret FROM person_scores"):
            nm = str(r["name"] or "").strip()
            if not nm:
                continue
            w20 = _f(r["win_rate_20d"])
            w5 = _f(r["win_rate_5d"])
            base = 0.5
            if w20 is not None:
                base = min(1.0, max(0.0, w20))
            elif w5 is not None:
                base = min(1.0, max(0.0, w5))
            win[nm] = base
    except Exception:  # noqa: BLE001
        pass

    sql = ("SELECT symbol, symbol_name, author, action, post_time FROM trades "
           "WHERE symbol IS NOT NULL AND symbol != ''")
    args: list = []
    if asof:
        sql += " AND post_time <= ?"
        args.append(asof)
    try:
        rows = con.execute(sql + " ORDER BY id", args).fetchall()
    except Exception:  # noqa: BLE001
        return {}

    today = datetime.now().strftime("%Y-%m-%d")
    out: dict = {}
    for r in rows:
        sym = str(r["symbol"] or "").strip().zfill(6)
        if len(sym) != 6 or not sym.isdigit():
            continue
        act = str(r["action"] or "").upper()
        if act.startswith("SELL") or any(w in act for w in ("卖", "减仓", "清仓")):
            continue          # 跟随买入方，卖出的不参与
        who = str(r["author"] or "").strip()
        if not who:
            continue
        # 2) 新近度衰减：当天 1.3 / 3 天内 1.1 / 10 天内 1.0 / 更早 0.6
        ts = str(r["post_time"] or "")
        day = ts[:10]
        if day >= today:
            rec = 1.3
        elif day >= (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"):
            rec = 1.1
        elif day >= (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"):
            rec = 1.0
        else:
            rec = 0.6
        # 3) 胜率权重：0.55（全败）~ 1.45（全胜）
        w = rec * (0.45 + 1.0 * win.get(who, 0.5))
        a = out.setdefault(sym, {"w": 0.0, "who": [], "win": 0.0, "name": None})
        a["w"] += w
        a["win"] += win.get(who, 0.5)
        if who and who not in a["who"]:
            a["who"].append(who)
        a["name"] = a["name"] or (r["symbol_name"] or None)
    for sym, a in out.items():
        n = len(a["who"]) or 1
        a["strength"] = a["w"]
        a["evidence"] = ["%d 位选手近%dd有买入" % (n, LOOKBACK_DAYS),
                         "含：%s" % "、".join(a["who"][:3]),
                         "选手均胜率 %.0f%%" % (a["win"] / n * 100)]
    return out


def sig_extref(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """外部参考：外部信源看多信号，按信源 tier 加权。

    口径说明：extref_signals 为人工沉淀、低频更新（看多信号集中在 2026-09-07/08，
    远早于 10 日回看窗口），故不按"近 N 日"过滤，只取 direction 含「多」且排除「中性」的行。
    asof：数据隔离边界 —— 只取 pub_date <= asof 的信号（pub_date 为信源发布日，可信）；
    pub_date 为空的旧行按"早已发布"处理照常参与。
    """
    sql = "SELECT symbol, symbol_name, src_name, src_tier, direction, pub_date " \
          "FROM extref_signals WHERE symbol IS NOT NULL AND symbol != ''"
    args: list = []
    if asof:
        sql += " AND (pub_date IS NULL OR pub_date = '' OR pub_date <= ?)"
        args.append(asof)
    rows = con.execute(sql, args).fetchall()
    tw = {"A": 1.0, "B": 0.6, "C": 0.3}
    out: dict = {}
    for r in rows:
        sym = str(r["symbol"] or "").strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        d = str(r["direction"] or "")
        if d and not any(w in d for w in ("多", "买", "推荐", "看好", "增")):
            continue
        a = out.setdefault(sym, {"w": 0.0, "src": [], "name": None})
        a["w"] += tw.get(str(r["src_tier"] or "B").upper(), 0.6)
        if r["src_name"] and r["src_name"] not in a["src"]:
            a["src"].append(r["src_name"])
        a["name"] = a["name"] or (r["symbol_name"] or None)
    for sym, a in out.items():
        a["strength"] = a["w"]
        a["evidence"] = ["%d 个信源看多" % len(a["src"]),
                         "含：%s" % "、".join(a["src"][:3])]
    return out


def sig_holders(con: sqlite3.Connection, asof: str | None = None) -> dict:
    """机构结构（原名「筹码集中」，2026-10-01 升级）：

    原只算「股东户数环比下降」，信息量太薄。升级为两件事合成：
      ① stock_holders（26128 行，**池内 160/160 全覆盖**）：最新一期前十大里的机构家数、
         机构合计持股占比（hold_ratio）、以及 hold_change 含"增持"的家数 —— 这是最硬的
         内部资料，比股东户数总数可读得多；
      ② stock_holder_num：股东户数环比下降（筹码趋集中）。
    """
    out: dict = {}
    # ① 前十大股东
    #  重要：同一股东会有 scope='free'/'total' 两行、且跨多个 end_date 重复出现，
    #  早期版本直接累加导致「机构合计持股 30334%」这种离谱值。这里按
    #  (symbol, end_date, holder_rank) 去重后再取最新一期末，且机构占比只累加该期前十大。
    try:
        sql = ("SELECT symbol, end_date, holder_rank, holder_name, is_org, holder_type, "
               "hold_ratio, hold_change FROM stock_holders "
               "WHERE symbol IS NOT NULL AND symbol != ''")
        rows = con.execute(sql).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    last: dict = {}
    seen: set = set()
    for r in rows:
        sym = str(r["symbol"] or "").strip().zfill(6)
        if len(sym) != 6 or not sym.isdigit():
            continue
        d = str(r["end_date"] or "")
        rank = _f(r["holder_rank"], 999) or 999.0
        key = (sym, d, rank)
        if key in seen:
            continue                     # free/total 双 scope 去重
        seen.add(key)
        a = last.setdefault(sym, {"date": "", "org_n": 0, "ratio": 0.0,
                                  "add_n": 0, "cut_n": 0, "name": None})
        if d > a["date"]:
            a.clear()
            a.update({"date": d, "org_n": 0, "ratio": 0.0, "add_n": 0, "cut_n": 0, "name": None})
        if rank > 10:
            continue                     # 只看前十大
        is_org = _f(r["is_org"], 0) or str(r["holder_type"] or "") in ("机构", "基金", "券商", "保险")
        if is_org:
            a["org_n"] += 1
            a["ratio"] += _f(r["hold_ratio"], 0.0) or 0.0
        chg = str(r["hold_change"] or "")
        if any(w in chg for w in ("增持", "加仓")):
            a["add_n"] += 1
        elif any(w in chg for w in ("减持", "减仓")):
            a["cut_n"] += 1
    # ② 股东户数环比
    try:
        hrows = con.execute(
            "SELECT symbol, end_date, holder_total_num FROM stock_holder_num "
            "WHERE symbol IS NOT NULL AND holder_total_num IS NOT NULL "
            "ORDER BY symbol, end_date").fetchall()
    except Exception:  # noqa: BLE001
        hrows = []
    lastn: dict = {}
    for r in hrows:
        sym = str(r["symbol"] or "").strip().zfill(6)
        if len(sym) != 6 or not sym.isdigit():
            continue
        n = _f(r["holder_total_num"])
        if n is None or n <= 0:
            continue
        lastn.setdefault(sym, []).append((str(r["end_date"] or ""), n))
    for sym, a in last.items():
        ratio = a["ratio"]                  # hold_ratio 本身就是百分数（27.18 = 27.18%）
        strength = ratio + 2.0 * a["add_n"] - 1.5 * a["cut_n"]
        ev = ["%s 前十大含机构 %d 家" % (a["date"] or "最新期", a["org_n"]),
              "机构合计持股 %.1f%%" % ratio]
        if a["add_n"]:
            ev.append("其中 %d 家增持" % a["add_n"])
        if a["cut_n"]:
            ev.append("%d 家减持" % a["cut_n"])
        lst = lastn.get(sym) or []
        if len(lst) >= 2:
            lst.sort()
            (_d0, n0), (d1, n1) = lst[-2], lst[-1]
            if n0 > 0:
                chg = (n1 - n0) / n0
                if chg < -0.005:
                    strength += -chg * 100
                    ev.append("股东户数较上期 %+.1f%%" % (chg * 100))
        if ratio < 1.0 and not a["add_n"] and not a["cut_n"] and len(lst) < 2:
            continue                        # 既无机构也无增减持、户数也没变 → 不占名额
        out[sym] = {"strength": strength, "evidence": ev, "name": a.get("name")}
    return out


# ---------------------------------------------------------------- 权重
def _default_weights() -> dict:
    """初始权重 = 先验系数归一化（不再等权；等权会让 1.3% 覆盖的外部参考和 98% 覆盖的技术面同权）。"""
    tot = sum(SIGNAL_PRIOR.get(k, 1.0) for k in SIGNAL_KEYS) or 1.0
    return {k: SIGNAL_PRIOR.get(k, 1.0) / tot for k in SIGNAL_KEYS}


def _drop_stale_signal_keys(con: sqlite3.Connection) -> None:
    """删掉上一代维度遗留的 signal 行（六路的 fund「龙虎榜资金」等）。只加不减会出事：

    残留一行就让 load_weights 的「口径一致性校验」（got != set(SIGNAL_KEYS)）永远不通过，
    库里存的 rescore 权重永远读不回来，每轮打分都退回先验 —— 2026-10-01 实测踩到。
    放在 ensure_tables 里自愈，这样重启服务（不重新生成）也能收敛到当前维度。
    """
    try:
        stale = [str(r["signal_key"]) for r in con.execute(
            "SELECT signal_key FROM predictor_score WHERE kind='signal'")]
    except Exception:  # noqa: BLE001
        return
    stale = [k for k in stale if k not in SIGNAL_KEYS]
    if stale:
        con.executemany("DELETE FROM predictor_score WHERE signal_key=?",
                        [(k,) for k in stale])
        con.commit()


def _cover_conf(hit_n: int) -> float:
    """覆盖置信度：命中票数越少 → 越接近 0（说明该信号对池子没代表性）。"""
    return 1.0 - math.exp(-(max(0, int(hit_n or 0)) / COVER_K))


def load_weights(con: sqlite3.Connection) -> dict:
    """读取当前生效权重。

    2026-10-01 修复：维度从六路扩到十二路后，predictor_score 里仍残留旧六路等权 1/6，
    与新七路的先验值混用 → 归一化后每路只剩 8.3%，十二路信息被稀释成"伪等权"。
    故这里做**口径一致性校验**：库里存的 signal key 集合必须与当前 SIGNALS 完全一致，
    否则（维度变更期）一律返回先验归一权重，绝不混用两个口径的权重。
    """
    w = _default_weights()
    try:
        rows = list(con.execute(
            "SELECT signal_key, weight FROM predictor_score WHERE kind='signal'"))
    except Exception:  # noqa: BLE001
        return w
    got = {str(r["signal_key"]) for r in rows if r["weight"] is not None}
    if not got or got != set(SIGNAL_KEYS):
        return w
    for r in rows:
        if r["signal_key"] in w and r["weight"] is not None:
            w[r["signal_key"]] = float(r["weight"])
    return w


def _shrunk_hit(n: int, hit: int) -> float:
    """贝叶斯收缩命中率：样本少 → 靠近 0.5 先验；样本多 → 靠近实盘表现。"""
    return (hit + PRIOR_HIT * PRIOR_N) / (n + PRIOR_N) if (n + PRIOR_N) > 0 else PRIOR_HIT


def _history_coverage(con: sqlite3.Connection) -> dict:
    """从历史 prediction_run.breadth_json 取各信号的历史最大命中票数（覆盖度）。

    为什么必须算覆盖度：2026-10-01 王总指出「外部参考 2 只、论坛情绪 9 只却和技术面 154 只
    拿一样权重，等于拿噪声当信号」。覆盖度校正就是治这个 —— 命中票数少的信号，
    先自己把权重往下压，再谈实盘胜率。
    """
    # 版本隔离：覆盖度只能取自**同一代引擎**的 run。v1.0 那几天的 breadth 里根本没有
    # 新维度的键，混进来会让它们覆盖度算 0 → 被压到 COVER_FLOOR，反过来把旧几路抬高
    # （2026-10-01 实测：v2.0 首日 rescore 后 tech 23% / follow 8.7% 反超 inst 6.6%，
    # 只因 inst 在 v1.0 里压根不存在）。跨版本比对口径本身就是错的。
    cov: dict = {}
    # 覆盖度同样只认主参考窗口内的 run（与 _aggregate 同一个尺子，否则两处又会变成两把尺）
    _ws = window_start()
    _f = " AND run_date >= ?" if _ws else ""
    try:
        rows_ver = con.execute(
            "SELECT breadth_json FROM prediction_run "
            "WHERE engine_ver=? AND breadth_json IS NOT NULL AND breadth_json != ''"
            + _f, ((ENGINE_VER,) + ((_ws,) if _ws else ()))).fetchall()
        rows = rows_ver if rows_ver else con.execute(
            "SELECT breadth_json FROM prediction_run "
            "WHERE breadth_json IS NOT NULL AND breadth_json != ''" + _f,
            ((_ws,) if _ws else ())).fetchall()
        for r in rows:
            try:
                b = json.loads(r["breadth_json"] or "{}")
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(b, dict):
                continue
            for k, v in b.items():
                if k.startswith("_") or k not in SIGNAL_KEYS:
                    continue          # _w/_prior 等快照字段不是命中票数，勿污染覆盖度
                if isinstance(v, (int, float)) and v > 0:
                    cov[k] = max(cov.get(k, 0), int(v))
    except Exception:  # noqa: BLE001
        pass
    return cov


# ---- 近两年逐日回放 → 技术面权重校准（王总 2026-10-06）--------------------------------
# 实盘复盘样本攒得太慢（一天只出几十条，且要等窗口到期），技术面却唯一能拿历史 K 线
# 逐日回放 —— 其余十一路没有历史快照，回测不了。
# 所以在「技术面实盘样本 < REPLAY_MIN_SAMPLE」时，技术面改用近两年逐日回放的命中率定权重。
# 口径与实盘逐字对齐：命中 = 跑赢候选池同期等权平均（与 prediction_item.hit_* 同一套算法）。
REPLAY_MIN_SAMPLE = 200
REPLAY_TECH_SIGNAL = "tech"


def _replay_stats(con: sqlite3.Connection) -> dict:
    """读 predictor_replay（近两年逐日回放），汇总各窗口的 (命中数, 样本数)。"""
    out = {}
    try:
        r = con.execute("SELECT scores_json FROM predictor_replay WHERE id=1").fetchone()
        if not r or not r["scores_json"]:
            return out
        d = json.loads(r["scores_json"]) or {}
        for w in ("t1", "t3", "t5", "t10"):
            n = h = 0
            for k, v in d.items():
                if k.startswith("school:") or not isinstance(v, dict):
                    continue                       # 流派级不重复计数，只吃策略级
                o = v.get(w) or {}
                nn = o.get("n") or 0
                if not nn:
                    continue
                hr = o.get("hit_rate")
                n += nn
                h += nn if hr is None else int(round(nn * hr))
            out[w] = (n, h)
    except Exception:  # noqa: BLE001
        pass
    return out


def _compute_weights(con: sqlite3.Connection) -> tuple:
    """按「先验系数 × 实盘命中率 × 覆盖置信度」算十二路权重，返回 (new_w, detail)。只算不写库。

    先验系数此前漏乘（2026-10-01 实测踩到）：rescore 只乘 hit_score×cov_blend，
    导致每次跑完都按"覆盖度"重新洗牌——游资上榜（prior 0.30，全十二路最低）
    会因为"覆盖 52 只 > 外部参考 2 只"被抬到权重第三，与先验设计正好相反。
    补上 prior 后，冷启动（无历史样本 → hit_score=1.0）时权重自然退化为"先验 × 覆盖置信度"。
    """
    cover = _history_coverage(con)
    rp = _replay_stats(con)          # 技术面实盘样本不足时兜底用（近两年回放）
    rows = con.execute("SELECT * FROM predictor_score WHERE kind='signal'").fetchall()
    raw, detail = {}, {}
    for r in rows:
        k = r["signal_key"]
        if k not in SIGNAL_KEYS:
            continue
        src = "实盘复盘"
        if k == REPLAY_TECH_SIGNAL and rp and \
                (int(r["n_t3"] or 0) + int(r["n_t5"] or 0) + int(r["n_t10"] or 0)) < REPLAY_MIN_SAMPLE:
            n3, h3 = rp.get("t3", (0, 0))
            n5, h5 = rp.get("t5", (0, 0))
            n10, h10 = rp.get("t10", (0, 0))
            src = "近两年逐日回放（%s 起）" % (rp.get("from") or "")
        else:
            n3, h3 = int(r["n_t3"] or 0), int(r["n_hit_t3"] or 0)
            n5, h5 = int(r["n_t5"] or 0), int(r["n_hit_t5"] or 0)
            n10, h10 = int(r["n_t10"] or 0), int(r["n_hit_t10"] or 0)
        s3 = _shrunk_hit(n3, h3)
        s5 = _shrunk_hit(n5, h5)
        s10 = _shrunk_hit(n10, h10)
        # 权重主轴 T+5（0.6）+ T+3（0.25）+ T+10（0.15）
        score = 0.6 * s5 + 0.25 * s3 + 0.15 * s10
        eff = max(0.0, 1.0 - math.exp(-(n3 + n5 + n10) / 40.0))   # 样本量置信度 0→1
        hit_score = 1.0 + eff * (score - 0.5) * 2.0              # 中性=1.0
        cov_n = cover.get(k, 0)
        cov_conf = _cover_conf(cov_n)                            # 覆盖置信度 0→1
        cov_blend = COVER_FLOOR + (1.0 - COVER_FLOOR) * cov_conf
        prior = SIGNAL_PRIOR.get(k, 1.0)
        raw[k] = prior * hit_score * cov_blend
        detail[k] = {"prior": prior, "hit_t3": s3, "hit_t5": s5, "hit_t10": s10,
                     "n3": n3, "n5": n5, "n10": n10, "conf": eff,
                     "cov_n": cov_n, "cov_conf": cov_conf, "cov_blend": cov_blend,
                     "src": src}
    if not raw:
        raw = {k: SIGNAL_PRIOR.get(k, 1.0) for k in SIGNAL_KEYS}
    # 归一化 → 限幅 → **再归一化**。
    #   第一道限幅加在 raw 上会失效：raw 的量级是 prior×hit×cov（≈0.15~1.35），
    #   十二路全都 > W_MAX，一夹就全等于 W_MAX，归一化后又变回"伪等权"，
    #   覆盖率差异（技术面 154 只 vs 外部参考 2 只）被抹平 —— 2026-10-01 实测踩到。
    #   故限幅放到归一化之后：夹的是最终权重百分比，语义才是"单路不超过 34% / 不低于 2%"。
    tot = sum(raw.values()) or 1.0
    norm_w = {k: v / tot for k, v in raw.items()}
    clipped = {k: max(W_MIN, min(W_MAX, v)) for k, v in norm_w.items()}
    tot2 = sum(clipped.values()) or 1.0
    new_w = {k: v / tot2 for k, v in clipped.items()}
    return new_w, detail


def rescore(con: sqlite3.Connection, log: bool = True) -> dict:
    """按「实盘命中率 × 覆盖置信度 × 先验系数」重算十二路信号权重，变动写日志。

    口径：
      - 实盘表现：以 T+5 命中率为主轴（0.6）+ T+3（0.25）+ T+10（0.15），贝叶斯收缩防过拟合；
      - 覆盖置信度：命中票数越少越不信任（外部参考 2 只 → 大幅降权；技术面 154 只 → 不降）；
      - 先验系数：新维度首版没有历史样本，靠先验（我对可信度的判断）定初始位置；
      - 三者相乘后限幅（W_MIN/W_MAX）+ 归一化。
    """
    new_w, detail = _compute_weights(con)

    old = load_weights(con)
    changes = []
    ts = _now()
    for k in SIGNAL_KEYS:
        before, after = old.get(k), new_w[k]
        if log and abs(after - before) >= ADJUST_LOG_EPS:
            d = detail.get(k, {})
            if not (d.get("n5") or d.get("n3") or d.get("n10")):
                # 无实盘样本：不打"命中率 50%"这种假数字，直接说明权重由先验 + 覆盖度决定
                reason = "无实盘复盘样本，权重由先验 × 覆盖度决定"
            else:
                base = ("T+5 命中率 %.1f%%（样本 %d）、T+3 %.1f%%、T+10 %.1f%%"
                        % (d.get("hit_t5", 0.5) * 100, d.get("n5", 0),
                           d.get("hit_t3", 0.5) * 100, d.get("hit_t10", 0.5) * 100))
                # 命中率来源自述：实盘复盘 vs 近两年逐日回放（王总 2026-10-06 定：以近两年为主参考）
                reason = ("近两年回放 " + base) if "回放" in str(d.get("src") or "") else base
            if d.get("cov_n"):
                reason += "；覆盖 %d 只（置信度 %.2f）" % (d["cov_n"], d.get("cov_conf", 0))
            # 口径自述：让人一眼知道这批权重是拿哪段时间的涨跌算出来的（呼应窗口调整）
            _wsd = window_start()
            reason = ("[近 %.1f 年窗口%s] " % (
                LOOKBACK_YEARS, ("自 %s 起" % _wsd) if _wsd else "")) + reason
            reason += " → 权重 %+.1f%%" % ((after - before) * 100)
            changes.append((_today(), k, "signal", before, after, reason, ts))
        meta = SIGNAL_META.get(k, {})
        con.execute(
            "INSERT INTO predictor_score (signal_key, kind, name, school, weight, weight_prev, updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(signal_key) DO UPDATE SET "
            "weight=excluded.weight, weight_prev=predictor_score.weight, updated_at=excluded.updated_at",
            (k, "signal", meta.get("name", k), None, after, before, ts))
    if changes:
        con.executemany(
            "INSERT INTO predictor_adjust_log (log_date,signal_key,kind,weight_before,weight_after,reason,created_at) "
            "VALUES (?,?,?,?,?,?,?)", changes)
    con.commit()
    return {"weights": new_w, "adjusted": len(changes), "detail": detail}


def _ensure_score_rows(con: sqlite3.Connection, items: list) -> None:
    """确保记账行存在：十二路信号 + 九流派 + 全部策略（后两类仅记录，不参与合成权重）。"""
    _drop_stale_signal_keys(con)
    rows = []
    for k in SIGNAL_KEYS:
        m = SIGNAL_META[k]
        rows.append((k, "signal", m["name"], None))
    schools = sorted({(s.get("school") or "其它") for s in (STRATEGIES or [])})
    for sc in schools:
        rows.append(("school:" + sc, "school", sc, sc))
    for s in (STRATEGIES or []):
        if not s.get("backtestable") or s.get("scope_limited") or s.get("needs"):
            continue
        rows.append((s.get("key"), "strategy", s.get("name") or s.get("key"), s.get("school")))
    for key, kind, name, school in rows:
        try:
            con.execute(
                "INSERT OR IGNORE INTO predictor_score (signal_key,kind,name,school,weight,weight_prev,updated_at) "
                "VALUES (?,?,?,?,NULL,NULL,?)", (key, kind, name, school, _now()))
        except Exception:  # noqa: BLE001
            pass
    con.commit()


# ---------------------------------------------------------------- 合成与生成
def _market_view(sig_stats: dict, senti: dict, weights: dict, top_sectors: list) -> tuple:
    """由信号统计合成当日研判（纯数据描述 + 强弱结论，不引入外部判断）。

    宽度口径用「多信号共振只数占比」而非技术面触发率 —— 后者因 86 条策略近乎全触发
    （99%）会变成恒为偏强的伪信号；前者才反映"有多少票被多路独立信号共同看多"。"""
    conv_n = sig_stats.get("conv_n", 0)
    pool_n = sig_stats.get("_pool", 1) or 1
    breadth = conv_n / pool_n * 100 if pool_n else 0
    if breadth >= 35:
        tone, style = "偏强", "多信号共振扩散，可积极选股"
    elif breadth >= 20:
        tone, style = "中性偏强", "结构性行情，资金聚焦少数板块"
    elif breadth >= 10:
        tone, style = "中性偏弱", "缩量分化，信号集中于强势股"
    else:
        tone, style = "偏弱", "信号稀少，宜降低仓位等待"
    parts = ["多信号共振 %d/%d 只（%.0f%%）" % (conv_n, pool_n, breadth)]
    if senti.get("net") is not None:
        parts.append("论坛情绪净多 %+d" % senti["net"])
    if sig_stats.get("inst_n"):
        parts.append("机构席位净买 %+.2f 亿" % sig_stats["inst_n"])
    view = "市场宽度%s → %s。依据：%s。" % (tone, style, "；".join(parts))
    sect = ("主线板块：%s" % "、".join(top_sectors[:3])) if top_sectors else "主线板块未形成明显聚焦"
    return view, sect


def _sector_of(con: sqlite3.Connection, syms: list) -> dict:
    """个股 → 板块（复用 board_flow 的映射思路：优先 stock_org，退回无）。"""
    out = {}
    if not syms:
        return out
    try:
        q = ",".join("?" * len(syms))
        for r in con.execute(
                "SELECT code, MAX(industry) AS industry FROM stock_org "
                "WHERE code IN (%s) GROUP BY code" % q, syms):
            if r["industry"]:
                out[str(r["code"])] = r["industry"]
    except Exception:  # noqa: BLE001
        pass
    return out


def generate(con: sqlite3.Connection, run_date: str | None = None,
             force: bool = False, progress=None) -> dict:
    """生成当日预测。

    run_date：预测日（默认 = 候选池 K 线的最后一根交易日）。已有同日记录且 force=False → 直接返回。
    """
    with _LOCK:
        if _STATUS["running"]:
            return {"ok": False, "error": "已有生成任务在跑，请稍候"}
        _STATUS.update(running=True, stage="准备", started_at=_now(), last_error=None)
        try:
            return _generate_inner(con, run_date, force, progress)
        except Exception as e:  # noqa: BLE001
            _STATUS["last_error"] = str(e)[:300]
            raise
        finally:
            _STATUS.update(running=False, stage="")


def _generate_inner(con, run_date, force, progress) -> dict:
    t0 = time.time()
    ensure_tables(con)
    _ensure_score_rows(con, None)

    # 1) 候选池 + K 线
    _STATUS["stage"] = "取候选池"
    codes, names, _boards = SS.universe(POOL_LIMIT)
    if not codes:
        return {"ok": False, "error": "候选池为空（positions / lhb_stocks 无数据）"}
    kc = load_kcache()
    bars_map = fetch_bars(codes, kc, force=force,
                          progress=(lambda a, b: progress and progress("抓 K 线", a, b)))
    bars_map = {c: v for c, v in bars_map.items() if len(v) >= MIN_BARS}
    td = run_date or _trade_date_of(bars_map)
    if not td:
        return {"ok": False, "error": "无法确定交易日"}

    if not force:
        row = con.execute("SELECT run_date FROM prediction_run WHERE run_date=?", (td,)).fetchone()
        if row:
            return {"ok": True, "run_date": td, "cached": True}

    # 1.5) 数据隔离（asof 截断）：预测日为 td，则一切信号只允许看见 td 及以前的数据。
    #   - K 线：截断到 td 收盘（补跑历史日时尤其关键——K 线缓存里可能有 td 之后的真实走势，
    #     不截断就等于"拿着后面的答案做前面的预测"）。
    #   - 龙虎榜 / 外部参考：sig_fund / sig_extref 内按 trade_date / pub_date <= td 过滤。
    #   - 论坛情绪 / 选手跟随 / 股东户数：库内时间戳不可靠（post_time 停在 2021 等），
    #     属"快照语义"，无法精确回溯 —— 在 run note 中如实标注，不假装隔离。
    _STATUS["stage"] = "数据隔离截断"
    bars_map = {c: [b for b in v if str(b.get("d", ""))[:10] <= td]
                for c, v in bars_map.items()}
    bars_map = {c: v for c, v in bars_map.items() if len(v) >= MIN_BARS}
    if not bars_map:
        return {"ok": False, "error": "截断到 %s 后候选池 K 线为空" % td}

    # 2) 六路信号
    _STATUS["stage"] = "算技术面"
    strat_list = _load_strategies()
    tech = sig_tech(bars_map, strat_list,
                    progress=(lambda a, b: progress and progress("算技术面", a, b)))
    _STATUS["stage"] = "汇总其它信号"
    senti_raw = sig_sentiment(con)
    inst_raw = sig_inst(con, asof=td)        # 龙虎榜「机构专用」席位（硬钱）
    lhb_raw = sig_lhb(con, asof=td)          # 龙虎榜游资/营业部席位（噪声级）
    follow_raw = sig_follow(con, asof=td)
    extref_raw = sig_extref(con, asof=td)
    holders_raw = sig_holders(con)
    ann_raw = sig_ann(con, asof=td)
    desk_raw = sig_desk(con, asof=td)
    policy_raw = sig_policy(con, asof=td)
    board_raw = sig_board(con, asof=td)
    overseas_raw = sig_overseas(con, asof=td)
    raw_signals = {
        "tech": tech, "holders": holders_raw, "ann": ann_raw, "desk": desk_raw,
        "inst": inst_raw, "policy": policy_raw, "board": board_raw,
        "sentiment": senti_raw, "overseas": overseas_raw, "follow": follow_raw,
        "extref": extref_raw, "lhb": lhb_raw,
    }
    # 只保留候选池内的票参与合成（池外票无 K 线，无法复盘校验，也不计入宽度统计）。
    # 否则 follow/holders 等全表信号会把池外票混入打分与「多信号共振」宽度统计。
    pool_set = set(bars_map.keys())
    for k in list(raw_signals):
        raw_signals[k] = {s: v for s, v in raw_signals[k].items() if s in pool_set}

    # 情绪净多（用于研判文案）
    net_senti = 0
    try:
        for r in con.execute(
                "SELECT sentiment_score FROM posts WHERE post_time >= ?",
                ((datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d"),)):
            v = _f(r["sentiment_score"], 0.0) or 0.0
            v = max(-1.0, min(1.0, v / 4.0))   # sentiment_score 约 -14~3，直接归一
            if v > SENT_POS:
                net_senti += 1
            elif v < SENT_NEG:
                net_senti -= 1
    except Exception:  # noqa: BLE001
        pass

    # 3) 强度归一化 + 加权合成
    _STATUS["stage"] = "合成打分"
    weights = load_weights(con)
    # 各信号内部先按百分位归一（同一信号内可比），再乘权重
    norm = {}
    for k, m in raw_signals.items():
        if not m:
            norm[k] = {}
            continue
        vals = sorted(_f(v.get("strength"), 0.0) or 0.0 for v in m.values())
        norm[k] = {sym: _pct_le(vals, _f(v.get("strength"), 0.0) or 0.0)
                   for sym, v in m.items()}
    scores: dict = defaultdict(float)
    hits_by_sym: dict = defaultdict(list)
    for k in SIGNAL_KEYS:
        w = weights.get(k, 0.0)
        for sym, p in norm.get(k, {}).items():
            if p <= 0:
                continue
            scores[sym] += w * p
            src = raw_signals[k].get(sym) or {}
            hit = {
                "key": k, "name": SIGNAL_META[k]["name"], "weight": round(w, 4),
                "pct": round(p, 3), "strength": round(_f(src.get("strength"), 0.0) or 0.0, 3),
                "evidence": src.get("evidence") or [],
            }
            if k == "tech":      # 带上逐策略/流派明细，供流派记分与"哪种方法更准"对比
                hit["strategies"] = src.get("strategies") or []
                hit["schools"] = src.get("schools") or []
            hits_by_sym[sym].append(hit)

    # 4) 排序取核心/观察（同分时技术面命中数多者优先）
    # 多信号共振加成：一只票被越多路独立信号命中，判断越稳（加权后再叠加固定加成，
    # 直接落地用户"每个部分都多多少少参与一下、综合对比"的要求）。
    for sym in list(scores):
        nsig = len(hits_by_sym.get(sym, []))
        if nsig >= 2:
            scores[sym] += 0.10 * (nsig - 1)
    ranked = sorted(scores.items(),
                    key=lambda kv: (-kv[1], -len(hits_by_sym[kv[0]]),
                                    -int(norm.get("tech", {}).get(kv[0], 0) * 100)))
    picked = ranked[:CORE_N + WATCH_N]
    if not picked:
        return {"ok": False, "error": "没有股票被任何信号命中，未生成预测"}

    # 5) 落库
    _STATUS["stage"] = "写库"
    pool_n = len(bars_map)
    sig_stats = {k: len(v or {}) for k, v in raw_signals.items()}
    sig_stats["_pool"] = pool_n
    sig_stats["conv_n"] = sum(1 for v in hits_by_sym.values() if len(v) >= 2)
    sig_stats["inst_n"] = sum((_f(v.get("strength"), 0) or 0) for v in inst_raw.values())
    sig_stats["lhb_n"] = sum((_f(v.get("strength"), 0) or 0) for v in lhb_raw.values())
    # 权重快照：前端信号条要同时显示「命中数」和「权重%」，两个口径必须对齐
    sig_stats["_w"] = {k: round(float(weights.get(k, 0.0)), 4) for k in SIGNAL_KEYS}
    sig_stats["_prior"] = {k: SIGNAL_PRIOR.get(k, 1.0) for k in SIGNAL_KEYS}
    sec_map = _sector_of(con, [s for s, _ in picked])
    top_sectors = [s for s, _ in Counter(sec_map.values()).most_common(5)]
    view, style = _market_view(sig_stats, {"net": net_senti}, weights, top_sectors)

    con.execute("DELETE FROM prediction_item WHERE run_date=?", (td,))
    con.execute("DELETE FROM prediction_run WHERE run_date=?", (td,))
    items = []
    for i, (sym, sc) in enumerate(picked):
        tier = "core" if i < CORE_N else "watch"
        k = bars_map.get(sym) or []
        entry = _f(k[-1].get("c")) if k else None
        nxt = _f(k[-1].get("c")) if k else None      # 次日开盘需 T+1 才有，复盘时回填
        hs = sorted(hits_by_sym[sym], key=lambda h: -h["weight"])
        reason = " + ".join("%s(%.0f%%)" % (h["name"], h["weight"] * 100) for h in hs[:3])
        ev = []
        for h in hs[:3]:
            ev.extend(h["evidence"][:1])
        if ev:
            reason += "｜" + "；".join(ev[:3])
        nm = names.get(sym) or sym
        caveat = "未到期"
        if "ST" in str(nm).upper() or "退" in str(nm):
            caveat = "⚠ ST/退市风险警示，短线波动限制 5%"
        elif entry is None:
            caveat = "⚠ 暂无完整K线，复盘收益待补"
        items.append((td, sym, nm, tier, i + 1, round(sc, 4),
                      json.dumps(hs, ensure_ascii=False), reason, entry, nxt,
                      None, None, None, None, None, None, None, None, None,
                      caveat, None))
    con.executemany(
        "INSERT OR REPLACE INTO prediction_item (run_date,symbol,name,tier,rank,score,"
        "hit_signals,reason,entry_close,next_open,ret_t1,ret_t3,ret_t5,"
        "bench_t1,bench_t3,bench_t5,hit_t1,hit_t3,hit_t5,caveat,reviewed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", items)
    con.execute(
        "INSERT OR REPLACE INTO prediction_run (run_date,created_at,engine_ver,pool_n,signal_n,"
        "core_n,watch_n,market_view,style_view,breadth_json,bench_json,status,note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (td, _now(), ENGINE_VER, pool_n, len(SIGNAL_KEYS),
         min(CORE_N, len(items)), max(0, len(items) - CORE_N),
         view, style, json.dumps(sig_stats, ensure_ascii=False), None, "ok",
         "候选池 %d 只；K线截至 %s（asof隔离：信号仅用 %s 及以前数据）；权重：%s" % (
             pool_n, td, td, "、".join("%s %.0f%%" % (SIGNAL_META[k]["name"], weights.get(k, 0) * 100)
                                      for k in SIGNAL_KEYS))))
    con.commit()
    try:
        res = rescore(con, log=True)     # 每次生成后刷新权重（把最新复盘结果纳入下一次判断）
        # 口径对齐：前端「N路信号贡献」读 breadth_json._w，「N路信号权重」读 predictor_score，
        # 二者必须同源，否则同一次生成里两张表数字对不上（2026-10-01 实测踩到）。
        w_after = (res or {}).get("weights") or {}
        if w_after:
            row = con.execute("SELECT breadth_json FROM prediction_run "
                              "WHERE run_date=?", (td,)).fetchone()
            if row:
                try:
                    b = json.loads(row["breadth_json"] or "{}") or {}
                    b["_w"] = {k: round(float(v), 4) for k, v in w_after.items()}
                    con.execute("UPDATE prediction_run SET breadth_json=? WHERE run_date=?",
                                (json.dumps(b, ensure_ascii=False), td))
                    con.commit()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "run_date": td, "core": min(CORE_N, len(items)),
            "watch": max(0, len(items) - CORE_N), "pool_n": pool_n,
            "elapsed": round(time.time() - t0, 2),
            "signals": sig_stats, "view": view, "style": style}


# ---------------------------------------------------------------- 复盘
def _bars_index(k: list) -> dict:
    return {str(b.get("d", ""))[:10]: i for i, b in enumerate(k)}


def review(con: sqlite3.Connection, force: bool = False, progress=None) -> dict:
    """对所有已生成预测做到期窗口复盘：T+1/T+3/T+5 收益 + 是否跑赢候选池平均。"""
    ensure_tables(con)
    runs = [r["run_date"] for r in con.execute(
        "SELECT run_date FROM prediction_run ORDER BY run_date")]
    if not runs:
        return {"ok": True, "reviewed": 0, "note": "尚无预测记录"}
    kc = load_kcache()
    codes, _names, _b = SS.universe(POOL_LIMIT)
    bars_map = fetch_bars(codes, kc, force=False,
                          progress=(lambda a, b: progress and progress("抓 K 线", a, b)))
    pool_curves = {c: _bars_index(v) for c, v in bars_map.items() if len(v) >= 30}

    done_items = 0
    done_runs = 0
    for rd in runs:
        items = con.execute("SELECT * FROM prediction_item WHERE run_date=?", (rd,)).fetchall()
        if not items:
            continue
        # 基准：候选池全部票在 T 起第 w 个交易日的同期平均收益
        bench = {}
        for w in WINDOWS:
            rets = []
            for c, idx in pool_curves.items():
                r = _window_ret(bars_map[c], idx.get(rd), w)
                if r is not None:
                    rets.append(r)
            bench[w] = (sum(rets) / len(rets)) if rets else None
        upd, pending = [], 0
        for it in items:
            sym = it["symbol"]
            k = bars_map.get(sym) or []
            idx = pool_curves.get(sym, {}).get(rd)
            entry = _f(it["entry_close"])
            if entry is None and idx is not None:
                entry = _f(k[idx].get("c"))
            row = {"hit": {}, "ret": {}, "caveat": [], "series": []}
            if idx is None or entry in (None, 0):
                pending += 1
                # ⚠️ 与下方 UPDATE 的 17 个占位符严格对齐：4 ret + 4 bench + 4 hit
                #    + series + caveat + reviewed_at + run_date + symbol = 17。
                #    旧版多给一个 None（18 值）→ 只要有一只票缺K线，executemany 整体
                #    报 "Incorrect number of bindings (17 vs 18)"，当天复盘/权重更新全失效。
                upd.append((None, None, None, None, None, None, None, None, None,
                            None, None, None, None,
                            "无K线数据", None, rd, sym))
                continue
            if idx + 1 < len(k):
                row["next_open"] = _f(k[idx + 1].get("o"))
            for w in WINDOWS:
                r = _window_ret(k, idx, w)
                if r is None:
                    continue          # 未到期
                b = bench.get(w)
                row["ret"][w] = r
                row["hit"][w] = (1 if (b is not None and r > b) else 0)
            # 逐日曲线（沉淀）：自预测日次日起，最多 MAX_SERIES 个交易日
            for j in range(idx + 1, min(len(k), idx + 1 + MAX_SERIES)):
                c = _f(k[j].get("c"))
                if c is None:
                    continue
                pc = _f(k[j - 1].get("c")) if j > 0 else entry
                row["series"].append({
                    "d": str(k[j].get("d", ""))[:10],
                    "c": round(c, 2),
                    "chg": round(c / pc - 1, 4) if pc else None,
                    "ret": round(c / entry - 1, 4),   # 相对预测日收盘的累计收益
                })
            if not row["ret"]:
                pending += 1
            # 除权粗检：首根交易日跌幅 < -11%（非跌停口径）→ 疑似除权，标注
            if idx + 1 < len(k):
                r1 = _window_ret(k, idx, 3)
                if r1 is not None and r1 < -0.115:
                    row["caveat"].append("首个交易日跌幅异常（疑似除权/停牌）")
            if row["hit"] and len(row["hit"]) == len(WINDOWS):
                done_items += 1
            upd.append((row["ret"].get(1), row["ret"].get(3), row["ret"].get(5), row["ret"].get(10),
                        bench.get(1), bench.get(3), bench.get(5), bench.get(10),
                        row["hit"].get(1), row["hit"].get(3), row["hit"].get(5), row["hit"].get(10),
                        json.dumps(row["series"], ensure_ascii=False),
                        ("未到期" if pending and not row["ret"] else
                         ("；".join(row["caveat"]) if row["caveat"] else None)),
                        _now(), rd, sym))
        con.executemany(
            "UPDATE prediction_item SET ret_t1=?,ret_t3=?,ret_t5=?,ret_t10=?,"
            "bench_t1=?,bench_t3=?,bench_t5=?,bench_t10=?,"
            "hit_t1=?,hit_t3=?,hit_t5=?,hit_t10=?,series_json=COALESCE(?,series_json),"
            "caveat=COALESCE(?,caveat),reviewed_at=? "
            "WHERE run_date=? AND symbol=?", upd)
        con.execute("UPDATE prediction_run SET bench_json=? WHERE run_date=?",
                    (json.dumps({"t%d" % w: bench.get(w) for w in WINDOWS},
                                ensure_ascii=False), rd))
        done_runs += 1
        if progress:
            progress("复盘", done_runs, len(runs))

    # 按信号/流派/策略聚合命中率
    _aggregate(con)
    con.commit()
    res = rescore(con, log=True)
    # ── 阶段3 合理性闸门（最小侵入·仅告警·不阻断·不改计算）──
    # 对已复盘的实现单日收益(ret_t1)做日内合理性校验（口径对齐 daily_return 闸门）
    _apply_reasonableness_gate_predictor(con)
    return {"ok": True, "reviewed": done_items, "runs": done_runs,
            "weights": res.get("weights"), "adjusted": res.get("adjusted")}


def _apply_reasonableness_gate_predictor(con) -> None:
    """对已复盘实现收益做日内合理性校验（仅告警，不改计算、不阻断）。

    - 闸门模块缺失或异常时静默跳过（生产零依赖、零污染）。
    - 仅取 1 交易日收益（ret_t1）做 daily_return 闸门，不重新计算任何公式。
    - 生产模式（test_mode=False）下越界只写告警，不抛异常、不影响返回。
    """
    try:
        try:
            from reasonableness_gate import run_gates  # 同目录平级导入
        except ImportError:
            from viewer.reasonableness_gate import run_gates  # 包内导入兜底
    except Exception:  # noqa: BLE001
        return
    try:
        rows = con.execute(
            "SELECT ret_t1 FROM prediction_item WHERE ret_t1 IS NOT NULL").fetchall()
        realized = [r["ret_t1"] for r in rows if r["ret_t1"] is not None]
        if not realized:
            return
        ctx = {"module": "viewer/predictor.py", "function": "review", "test_mode": False}
        run_gates({"daily_returns": realized}, ctx)
    except Exception:  # noqa: BLE001
        # 生产模式闸门不应抛；任何异常都不影响业务返回
        pass


def _window_ret(k: list, idx: int | None, w: int) -> float | None:
    """从 idx 起第 w 个交易日的收益（相对 idx 收盘）。未到期返回 None。"""
    if idx is None:
        return None
    j = idx + w
    if j >= len(k) or idx < 0:
        return None
    c0 = _f(k[idx].get("c"))
    c1 = _f(k[j].get("c"))
    if not c0 or c1 is None:
        return None
    return c1 / c0 - 1.0


def _aggregate(con: sqlite3.Connection) -> dict:
    """把每一条已复盘记录回填到信号/流派/策略三个层级的记分表。"""
    _ensure_score_rows(con, None)
    con.execute("UPDATE predictor_score SET n_t1=0,n_hit_t1=0,sum_ret_t1=0,"
                "n_t3=0,n_hit_t3=0,sum_ret_t3=0,"
                "n_t5=0,n_hit_t5=0,sum_ret_t5=0,n_t10=0,n_hit_t10=0,sum_ret_t10=0 "
                "WHERE kind IN ('signal','school','strategy')")
    # 主参考窗口（王总 2026-10-06）：早于 window_start 的复盘样本不再参与权重统计 ——
    # 太早的样本对当前策略没参考价值，混进来只会把近期特征冲淡。样本本身不删，仍留库可查。
    _ws = window_start()
    _SQL = ("SELECT run_date,symbol,ret_t1,ret_t3,ret_t5,ret_t10,"
            "hit_t1,hit_t3,hit_t5,hit_t10,hit_signals FROM prediction_item")
    rows = con.execute(_SQL + (" WHERE run_date >= ?" if _ws else ""),
                       ((_ws,) if _ws else ())).fetchall()
    # 用 K 线重算「每只票在其预测日触发了哪些策略」代价太高 → 只对技术面用存储的命中明细，
    # 其余信号用 item 上的 hit_signals 中的 key 归属。流派/策略层面依赖 hit_signals 里的
    # strategy 列表（生成时已随 tech 信号写入，格式见下）。
    agg: dict = defaultdict(lambda: {"n1": 0, "h1": 0, "s1": 0.0,
                                     "n3": 0, "h3": 0, "s3": 0.0,
                                     "n5": 0, "h5": 0, "s5": 0.0,
                                     "n10": 0, "h10": 0, "s10": 0.0})
    for r in rows:
        try:
            hs = json.loads(r["hit_signals"] or "[]")
        except Exception:  # noqa: BLE001
            hs = []
        keys = []
        for h in hs:
            keys.append(h.get("key"))
            for st in (h.get("strategies") or []):
                keys.append(st)
            for sc in (h.get("schools") or []):
                keys.append("school:" + sc)
        for kk in set(x for x in keys if x):
            a = agg[kk]
            for w, (nf, hf, sf) in ((1, ("n1", "h1", "s1")), (3, ("n3", "h3", "s3")),
                                    (5, ("n5", "h5", "s5")), (10, ("n10", "h10", "s10"))):
                ret = r["ret_t%d" % w]
                hit = r["hit_t%d" % w]
                if ret is None or hit is None:
                    continue
                a[nf] += 1
                a[hf] += int(hit)
                a[sf] += float(ret)
    _aggregate_window = {"start": _ws or "全部历史", "items": len(rows),
                         "years": LOOKBACK_YEARS}
    for kk, a in agg.items():
        con.execute(
            "UPDATE predictor_score SET n_t1=?,n_hit_t1=?,sum_ret_t1=?,"
            "n_t3=?,n_hit_t3=?,sum_ret_t3=?,"
            "n_t5=?,n_hit_t5=?,sum_ret_t5=?,n_t10=?,n_hit_t10=?,sum_ret_t10=?,updated_at=? "
            "WHERE signal_key=?", (a["n1"], a["h1"], round(a["s1"], 4),
                                   a["n3"], a["h3"], round(a["s3"], 4),
                                   a["n5"], a["h5"], round(a["s5"], 4),
                                   a["n10"], a["h10"], round(a["s10"], 4), _now(), kk))
    con.commit()
    return dict(agg)


# ---------------------------------------------------------------- 前端载荷
def _score_row(r) -> dict:
    def wr(n, h):
        n, h = int(n or 0), int(h or 0)
        return {"n": n, "hit": h, "rate": round(h / n, 4) if n else None}
    return {
        "key": r["signal_key"], "kind": r["kind"], "name": r["name"], "school": r["school"],
        "t1": wr(r["n_t1"], r["n_hit_t1"]),
        "t3": wr(r["n_t3"], r["n_hit_t3"]), "t5": wr(r["n_t5"], r["n_hit_t5"]),
        "t10": wr(r["n_t10"], r["n_hit_t10"]),
        "avg_ret_t1": round((r["sum_ret_t1"] or 0) / r["n_t1"], 4) if r["n_t1"] else None,
        "avg_ret_t3": round((r["sum_ret_t3"] or 0) / r["n_t3"], 4) if r["n_t3"] else None,
        "avg_ret_t5": round((r["sum_ret_t5"] or 0) / r["n_t5"], 4) if r["n_t5"] else None,
        "avg_ret_t10": round((r["sum_ret_t10"] or 0) / r["n_t10"], 4) if r["n_t10"] else None,
        "weight": r["weight"], "weight_prev": r["weight_prev"],
    }


def today_payload(con: sqlite3.Connection | None = None) -> dict:
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        rd = con.execute("SELECT MAX(run_date) AS d FROM prediction_run").fetchone()
        rid = rd["d"] if rd else None
        if not rid:
            return {"ok": True, "has_data": False, "signals": SIGNALS,
                    "engine_ver": ENGINE_VER, "status": status_snapshot()}
        run = con.execute("SELECT * FROM prediction_run WHERE run_date=?", (rid,)).fetchone()
        items = con.execute("SELECT * FROM prediction_item WHERE run_date=? ORDER BY rank",
                            (rid,)).fetchall()
        out_items = []
        for it in items:
            try:
                hs = json.loads(it["hit_signals"] or "[]")
            except Exception:  # noqa: BLE001
                hs = []
            try:
                series = json.loads(it["series_json"] or "[]")
            except Exception:  # noqa: BLE001
                series = []
            out_items.append({
                "symbol": it["symbol"], "name": it["name"], "tier": it["tier"],
                "rank": it["rank"], "score": it["score"], "signals": hs,
                "reason": it["reason"], "entry_close": it["entry_close"],
                "next_open": it["next_open"],
                "ret": {"t1": it["ret_t1"], "t3": it["ret_t3"], "t5": it["ret_t5"], "t10": it["ret_t10"]},
                "bench": {"t1": it["bench_t1"], "t3": it["bench_t3"], "t5": it["bench_t5"], "t10": it["bench_t10"]},
                "hit": {"t1": it["hit_t1"], "t3": it["hit_t3"], "t5": it["hit_t5"], "t10": it["hit_t10"]},
                "series": series,
                "caveat": it["caveat"],
            })
        # 最近一次权重调整理由（用于「我在自我修正」的可视化）
        adj = [dict(r) for r in con.execute(
            "SELECT * FROM predictor_adjust_log ORDER BY id DESC LIMIT 8").fetchall()]
        stale = _pred_stale(rid)
        out = {
            "ok": True, "has_data": True, "run_date": rid,
            "created_at": run["created_at"], "engine_ver": run["engine_ver"],
            "pool_n": run["pool_n"], "core_n": run["core_n"], "watch_n": run["watch_n"],
            "market_view": run["market_view"], "style_view": run["style_view"],
            "breadth": json.loads(run["breadth_json"] or "{}"),
            "bench": json.loads(run["bench_json"] or "null"),
            "status": run["status"], "note": run["note"],
            "items": out_items, "adjust_log": adj,
            "signals": SIGNALS, "windows": list(WINDOWS),
            "engine_status": status_snapshot(),
        }
        if stale:
            out["stale"] = True
            out["last_trade_date"] = stale["last_trade_date"]
            out["lag_days"] = stale["lag_days"]
            out["stale_reason"] = stale["reason"]
        else:
            out["stale"] = False
            out["stale_reason"] = ""
        return out
    finally:
        if own:
            con.close()


def _db_latest_data_date() -> str | None:
    """数据库里实际「落库过数据」的最新日期 = MAX(prediction_run.run_date, auction_snapshot.date)。

    用于「最近有数据的交易日」等 UI 字段的口径：只报真实落库过的日期，
    绝不用日历里「理论上存在但还没产生数据」的交易日（如今天 02:25 时的 10-09）。
    forward-backfill + 同天对账已保证 DB 日期不滞后，故该值即「真实最近有数据的交易日」。
    """
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        try:
            d1 = con.execute("SELECT MAX(run_date) AS m FROM prediction_run").fetchone()["m"]
            d2 = con.execute("SELECT MAX(date) AS m FROM auction_snapshot").fetchone()["m"]
        finally:
            con.close()
        cands = [x for x in (d1, d2) if x]
        return max(cands) if cands else None
    except Exception:  # noqa: BLE001
        return None


def _pred_stale(run_date: str | None) -> dict | None:
    """预测是否滞后于最近交易日 → 前端标题打「数据截至 MM-DD」角标 + 一句人话说明。

    触发条件：最近交易日 > 最新一份预测的 run_date（也就是「今天开盘了，预测还没补上」）。
    取不到最近交易日（日历/网络不可用）就静默返回 None —— **宁可不标，也不许标错**：
    标错的代价是用户按错日期下判断，比什么都不说更糟（2026-10-05 时效断档的教训）。
    """
    if not run_date:
        return None
    try:
        import datetime as _dt
        from emapi import latest_valuation_date
        from auction import latest_trade_date as _au_ltd
        ltd = latest_valuation_date()
        real_today = _au_ltd()           # 权威交易日历（已修跨日失效）
        # 取「估值披露日」与「真实交易日」里更晚的那个作参照：
        # 东财估值表(RPT_VALUEANALYSIS_DET)可能晚于交易日才披露，不能因为它没披露就漏标陈旧。
        ref = max([x for x in (str(ltd), str(real_today)) if x], default=None)
        # 【最新交易日口径 · 点4】「最近有数据的交易日」必须来自数据库实际落库的日期：
        # 把 ref 上限钳到 DB 里真实有数据的最新日期，避免把「日历存在但尚无数据」的交易日
        # （如今天 02:25 时的 10-09）误报成"最近有数据的交易日"。forward-backfill + 同天对账
        # 已保证 DB 日期不滞后，故钳制后 ref 即"真实最近有数据的交易日"。
        data_latest = _db_latest_data_date()
        if data_latest and (ref is None or ref > data_latest):
            ref = data_latest
        if not ref or ref <= str(run_date):
            return None
        d_pred = _dt.date.fromisoformat(str(run_date)[:10])
        d_ref = _dt.date.fromisoformat(str(ref)[:10])
        lag = max(0, (d_ref - d_pred).days)
        mmdd = str(run_date)[5:10]
        return {
            "last_trade_date": str(ref),
            "lag_days": lag,
            "reason": ("数据截至 %s：最近交易日 %s 的预测还没生成，下面是最近一次运行（%s）的结果，"
                       "生成/调度跑完这一日就补齐" % (mmdd, str(ref)[5:10], mmdd)),
        }
    except Exception:  # noqa: BLE001 —— 日历取不到就当不陈旧，绝不抛到接口外
        return None


def scores_payload(con: sqlite3.Connection | None = None) -> dict:
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        rows = con.execute("SELECT * FROM predictor_score").fetchall()
        sig, sch, stg = [], [], []
        for r in rows:
            d = _score_row(r)
            if r["kind"] == "signal":
                sig.append(d)
            elif r["kind"] == "school":
                sch.append(d)
            else:
                stg.append(d)
        order = {k: i for i, k in enumerate(SIGNAL_KEYS)}
        sig.sort(key=lambda x: order.get(x["key"], 99))
        sch.sort(key=lambda x: (-(x["t5"]["rate"] if x["t5"]["rate"] is not None else -1),
                                -(x["t5"]["n"] or 0)))
        stg.sort(key=lambda x: (-(x["t5"]["rate"] if x["t5"]["rate"] is not None else -1),
                                -(x["t5"]["n"] or 0)))
        return {"ok": True, "signals": sig, "schools": sch, "strategies": stg,
                "windows": list(WINDOWS)}
    finally:
        if own:
            con.close()


def track_payload(con: sqlite3.Connection | None = None) -> dict:
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        runs = con.execute(
            "SELECT run_date, core_n, watch_n, pool_n, market_view FROM prediction_run "
            "ORDER BY run_date").fetchall()
        out = []
        cum = {"t1": [0, 0], "t3": [0, 0], "t5": [0, 0], "t10": [0, 0]}   # [样本, 命中]
        for r in runs:
            rd = r["run_date"]
            items = con.execute(
                "SELECT tier, ret_t1, ret_t3, ret_t5, ret_t10, hit_t1, hit_t3, hit_t5, hit_t10 "
                "FROM prediction_item WHERE run_date=?", (rd,)).fetchall()
            day = {"run_date": rd, "core_n": r["core_n"], "watch_n": r["watch_n"],
                   "pool_n": r["pool_n"], "market_view": r["market_view"],
                   "core": {}, "watch": {}, "all": {}}
            for w in (1, 3, 5, 10):
                for tier in ("core", "watch", "all"):
                    sel = [x for x in items if tier == "all" or x["tier"] == tier]
                    rets = [_f(x["ret_t%d" % w]) for x in sel]
                    rets = [x for x in rets if x is not None]
                    hits = [x["hit_t%d" % w] for x in sel if x["hit_t%d" % w] is not None]
                    day[tier]["t%d" % w] = {
                        "n": len(rets),
                        "avg_ret": round(sum(rets) / len(rets), 4) if rets else None,
                        "hit_rate": round(sum(hits) / len(hits), 4) if hits else None,
                    }
                    if tier == "all":
                        cum["t%d" % w][0] += len(rets)
                        cum["t%d" % w][1] += sum(hits)
            out.append(day)
        summary = {}
        for w in (1, 3, 5, 10):
            n, h = cum["t%d" % w]
            summary["t%d" % w] = {"n": n, "hit": h,
                                  "hit_rate": round(h / n, 4) if n else None}
        return {"ok": True, "days": out, "summary": summary,
                "runs": len(out), "windows": list(WINDOWS)}
    finally:
        if own:
            con.close()


# ---------------------------------------------------------------- 历史查询（日期筛选 + 区间沉淀）
def _day_windows(con, rd: str) -> dict:
    """某一天各窗口的命中率 / 平均收益（用于 dates 清单与 history 汇总）。"""
    out = {}
    items = con.execute(
        "SELECT tier, ret_t1, ret_t3, ret_t5, ret_t10, hit_t1, hit_t3, hit_t5, hit_t10 "
        "FROM prediction_item WHERE run_date=?", (rd,)).fetchall()
    for w in (1, 3, 5, 10):
        rets, hits = [], []
        for x in items:
            r = _f(x["ret_t%d" % w])
            h = x["hit_t%d" % w]
            if r is not None:
                rets.append(r)
            if h is not None:
                hits.append(int(h))
        out["t%d" % w] = {
            "n": len(rets),
            "avg_ret": round(sum(rets) / len(rets), 4) if rets else None,
            "hit_rate": round(sum(hits) / len(hits), 4) if hits else None,
        }
    return out


def dates_payload(con: sqlite3.Connection | None = None) -> dict:
    """可选预测日清单（最早=首份预测日）+ 每日三窗口摘要，供前端日期选择器。"""
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        runs = con.execute(
            "SELECT run_date, core_n, watch_n, pool_n, market_view, created_at, "
            "status, note FROM prediction_run ORDER BY run_date").fetchall()
        dates = []
        for r in runs:
            rd = r["run_date"]
            dates.append({
                "run_date": rd, "core_n": r["core_n"], "watch_n": r["watch_n"],
                "pool_n": r["pool_n"], "market_view": r["market_view"],
                "created_at": r["created_at"], "status": r["status"], "note": r["note"],
                "windows": _day_windows(con, rd),
            })
        first = dates[0]["run_date"] if dates else None
        last = dates[-1]["run_date"] if dates else None
        return {"ok": True, "dates": dates, "first": first, "last": last,
                "count": len(dates), "windows": list(WINDOWS)}
    finally:
        if own:
            con.close()


def history_payload(con: sqlite3.Connection | None = None, run_date: str | None = None,
                    range_days: int = 0, window: int = 5) -> dict:
    """按日期回看（mode=day）或按区间汇总（mode=range）。

    - day：某一天 10 核心 + 10 观察的完整载荷（研判/信号/逐日曲线/复盘）。
    - range：最近 N 天（0=全部）所有预测日的汇总表现 + 逐日沉淀清单。
    两种模式都把每个窗口（3/5/10）的收益与命中随行下发，前端切换窗口不重复请求。
    """
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        all_runs = [r["run_date"] for r in con.execute(
            "SELECT run_date FROM prediction_run ORDER BY run_date").fetchall()]
        if not all_runs:
            return {"ok": True, "has_data": False, "windows": list(WINDOWS),
                    "signals": SIGNALS, "status": status_snapshot()}
        # 决定本次包含哪些 run_date
        if range_days and range_days > 0:
            inc = all_runs[-range_days:]
            mode = "range"
        else:
            rd = run_date or all_runs[-1]
            if rd not in all_runs:
                rd = all_runs[-1]
            inc = [rd]
            mode = "day"

        # 汇总各窗口
        cum = {w: [0, 0, 0.0] for w in WINDOWS}   # [样本, 命中, 累计收益]
        day_summ = []
        flat = []
        for rd in inc:
            items = con.execute(
                "SELECT * FROM prediction_item WHERE run_date=? ORDER BY rank", (rd,)).fetchall()
            dw = _day_windows(con, rd)
            day_summ.append({"run_date": rd, "windows": dw,
                             "core_n": len([x for x in items if x["tier"] == "core"]),
                             "watch_n": len([x for x in items if x["tier"] == "watch"])})
            for it in items:
                sigs = []
                try:
                    sigs = json.loads(it["hit_signals"] or "[]")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    series = json.loads(it["series_json"] or "[]")
                except Exception:  # noqa: BLE001
                    series = []
                rec = {
                    "run_date": rd, "symbol": it["symbol"], "name": it["name"],
                    "tier": it["tier"], "rank": it["rank"], "score": it["score"],
                    "signals": sigs, "reason": it["reason"],
                    "entry_close": it["entry_close"], "next_open": it["next_open"],
                    "ret": {"t1": it["ret_t1"], "t3": it["ret_t3"], "t5": it["ret_t5"], "t10": it["ret_t10"]},
                    "bench": {"t1": it["bench_t1"], "t3": it["bench_t3"], "t5": it["bench_t5"], "t10": it["bench_t10"]},
                    "hit": {"t1": it["hit_t1"], "t3": it["hit_t3"], "t5": it["hit_t5"], "t10": it["hit_t10"]},
                    "series": series, "caveat": it["caveat"],
                }
                flat.append(rec)
                for w in WINDOWS:
                    r = _f(it["ret_t%d" % w])
                    h = it["hit_t%d" % w]
                    if r is not None:
                        cum[w][0] += 1
                        cum[w][2] += float(r)
                    if h is not None:
                        cum[w][1] += int(h)
        summary = {}
        for w in WINDOWS:
            n, h, s = cum[w]
            summary["t%d" % w] = {"n": n, "hit": h,
                                  "hit_rate": round(h / n, 4) if n else None,
                                  "avg_ret": round(s / n, 4) if n else None}

        out = {"ok": True, "has_data": True, "mode": mode, "window": window,
               "windows": list(WINDOWS), "run_dates": inc, "summary": summary,
               "days": day_summ, "items": flat if mode == "range" else None,
               "signals": SIGNALS, "status": status_snapshot()}

        if mode == "day":
            rd = inc[0]
            run = con.execute("SELECT * FROM prediction_run WHERE run_date=?", (rd,)).fetchall()
            run = run[0] if run else None
            items = con.execute(
                "SELECT * FROM prediction_item WHERE run_date=? ORDER BY rank", (rd,)).fetchall()
            out_items = []
            for it in items:
                try:
                    hs = json.loads(it["hit_signals"] or "[]")
                except Exception:  # noqa: BLE001
                    hs = []
                try:
                    series = json.loads(it["series_json"] or "[]")
                except Exception:  # noqa: BLE001
                    series = []
                out_items.append({
                    "symbol": it["symbol"], "name": it["name"], "tier": it["tier"],
                    "rank": it["rank"], "score": it["score"], "signals": hs,
                    "reason": it["reason"], "entry_close": it["entry_close"],
                    "next_open": it["next_open"],
                    "ret": {"t1": it["ret_t1"], "t3": it["ret_t3"], "t5": it["ret_t5"], "t10": it["ret_t10"]},
                    "bench": {"t1": it["bench_t1"], "t3": it["bench_t3"], "t5": it["bench_t5"], "t10": it["bench_t10"]},
                    "hit": {"t1": it["hit_t1"], "t3": it["hit_t3"], "t5": it["hit_t5"], "t10": it["hit_t10"]},
                    "series": series, "caveat": it["caveat"],
                })
            adj = [dict(r) for r in con.execute(
                "SELECT * FROM predictor_adjust_log ORDER BY id DESC LIMIT 8").fetchall()]
            out.update({
                "run_date": rd,
                "created_at": run["created_at"] if run else None,
                "engine_ver": run["engine_ver"] if run else ENGINE_VER,
                "pool_n": run["pool_n"] if run else 0,
                "core_n": run["core_n"] if run else 0,
                "watch_n": run["watch_n"] if run else 0,
                "market_view": run["market_view"] if run else "",
                "style_view": run["style_view"] if run else "",
                "breadth": json.loads(run["breadth_json"] or "{}") if run else {},
                "bench": json.loads(run["bench_json"] or "null") if run else None,
                "status": run["status"] if run else None,
                "note": run["note"] if run else None,
                "items": out_items,
                "adjust_log": adj,
            })
        return out
    finally:
        if own:
            con.close()


def latest_summary(con: sqlite3.Connection | None = None) -> dict:
    """给「今日看板」用的一行摘要。"""
    own = con is None
    con = con or connect()
    try:
        ensure_tables(con)
        rd = con.execute("SELECT MAX(run_date) AS d FROM prediction_run").fetchone()
        rid = rd["d"] if rd else None
        if not rid:
            return {"ok": True, "has_data": False}
        r = con.execute("SELECT * FROM prediction_run WHERE run_date=?", (rid,)).fetchone()
        n = con.execute("SELECT COUNT(*) AS c FROM prediction_item WHERE run_date=?",
                        (rid,)).fetchone()["c"]
        sc = con.execute(
            "SELECT COUNT(*) AS n, SUM(hit_t1) AS h FROM prediction_item "
            "WHERE hit_t1 IS NOT NULL").fetchone()
        return {"ok": True, "has_data": True, "run_date": rid, "n": n,
                "market_view": r["market_view"],
                "hit_rate_t1": (round(sc["h"] / sc["n"], 4)
                                if sc["n"] and sc["h"] is not None else None),
                "n_reviewed": sc["n"] or 0}
    finally:
        if own:
            con.close()


# ---------------------------------------------------------------- 历史回放
def backtest(days: int = 60, pool_limit: int = POOL_LIMIT) -> dict:
    """技术面历史回放（离线命令行用）。

    对过去 days 个交易日，逐日构造「截至该日」的 K 线切片，用 build_signals 取当日触发，
    统计各流派 / 各策略的 T+3 / T+5 / T+10 命中率（命中 = 跑赢候选池同期等权平均）。

    只回放技术面 —— 情绪 / 资金 / 跟随 / 外部信号的表里没有历史快照，无法回测。
    """
    con = connect()
    try:
        codes, _names, _b = SS.universe(pool_limit)
        kc = load_kcache()
        bars_map = fetch_bars(codes, kc, force=False)
        # 回放天数自适应：按候选池 K 线长度的**中位数**算可支撑天数。
        # 不用最短值 —— 池子里偶有上市不足百日的新股（len=1），会把可回放天数压成 0。
        _L = sorted(len(v) for v in bars_map.values())
        avail = max(0, (_L[len(_L) // 2] if _L else 0) - MIN_BARS - 5)
        if days > avail:
            print("[predictor] 回放天数 %d 超出 K 线可支撑长度 %d，自动收到 %d（≈%.1f 年）"
                  % (days, avail, avail, avail / 244.0), flush=True)
            days = avail
        bars_map = {c: v for c, v in bars_map.items() if len(v) >= MIN_BARS + days + 10}
        strat_list = _load_strategies()
        if not bars_map:
            return {"ok": False, "error": "K 线不足，无法回放"}
        ref = max(bars_map.values(), key=len)
        n_end = len(ref)
        # 预计算每票的策略触发序列
        tech_seq = {c: sig_tech_replay(v, strat_list) for c, v in bars_map.items()}
        res: dict = defaultdict(lambda: {"n1": 0, "h1": 0, "n3": 0, "h3": 0, "n5": 0, "h5": 0,
                                         "n10": 0, "h10": 0, "s10": 0.0,
                                         "s1": 0.0, "s3": 0.0, "s5": 0.0})
        start = max(MIN_BARS, n_end - days - 5)
        for idx in range(start, n_end - 1):
            d = str(ref[idx].get("d", ""))[:10]
            # 当日基准：全池同期等权平均
            bench = {}
            for w in WINDOWS:
                rr = [_window_ret(v, idx, w) for v in bars_map.values()]
                rr = [x for x in rr if x is not None]
                bench[w] = (sum(rr) / len(rr)) if rr else None
            for c, v in bars_map.items():
                if idx >= len(v):
                    continue
                seq = tech_seq.get(c) or {}
                keys = []
                for s in strat_list:
                    k = s.get("key")
                    b = seq.get(k)
                    if b and idx < len(b) and b[idx]:
                        keys.append(k)
                        keys.append("school:" + (s.get("school") or "其它"))
                if not keys:
                    continue
                for kk in set(keys):
                    a = res[kk]
                    for w, (nf, hf, sf) in ((3, ("n3", "h3", "s3")), (5, ("n5", "h5", "s5")),
                                            (10, ("n10", "h10", "s10"))):
                        r = _window_ret(v, idx, w)
                        if r is None or bench[w] is None:
                            continue
                        a[nf] += 1
                        a[hf] += 1 if r > bench[w] else 0
                        a[sf] += r
        out = {}
        for kk, a in res.items():
            out[kk] = {
                "t3": {"n": a["n3"], "hit_rate": round(a["h3"] / a["n3"], 4) if a["n3"] else None,
                       "avg_ret": round(a["s3"] / a["n3"], 4) if a["n3"] else None},
                "t5": {"n": a["n5"], "hit_rate": round(a["h5"] / a["n5"], 4) if a["n5"] else None,
                       "avg_ret": round(a["s5"] / a["n5"], 4) if a["n5"] else None},
                "t10": {"n": a["n10"], "hit_rate": round(a["h10"] / a["n10"], 4) if a["n10"] else None,
                        "avg_ret": round(a["s10"] / a["n10"], 4) if a["n10"] else None},
            }
        _d0 = str(ref[start].get("d", ""))[:10]
        _d1 = str(ref[n_end - 2].get("d", ""))[:10]
        try:
            # 只留一条「近两年逐日回放」快照（id=1），权重校准读它就够
            con.execute("DELETE FROM predictor_replay WHERE id=1")
            con.execute(
                "INSERT INTO predictor_replay (id,window_label,days,pool,start_date,end_date,"
                "scores_json,computed_at) VALUES (1,?,?,?,?,?,?,?)",
                ("y2", days, len(bars_map), _d0, _d1,
                 json.dumps(out, ensure_ascii=False), _now()))
            con.commit()
            print("[predictor] 近两年逐日回放已落库：%s ~ %s，%d 个交易日 / %d 只票"
                  % (_d0, _d1, days, len(bars_map)), flush=True)
        except Exception as exc:  # noqa: BLE001
            print("[predictor] 回放结果落库失败（不影响本次统计）：%s" % exc, flush=True)
        return {"ok": True, "days": days, "pool": len(bars_map), "start": _d0,
                "end": _d1, "scores": out, "saved": True}
    finally:
        con.close()


# ---------------------------------------------------------------- 调度钩子
_DAILY_DONE: dict = {"date": None, "last": None, "fail": 0}

# 缺日补跑（catch-up）状态：前端轮询 /api/prediction/status 可见进度
_CATCHUP: dict = {"running": False, "stage": "", "total": 0, "done": 0,
                  "dates": [], "current": None, "error": None, "finished_at": None}
_CATCH_MAX = 10          # 单次补跑最多补几个交易日（防首跑回溯几年）
_CATCH_LOCK = threading.Lock()


def _catchup_status() -> dict:
    return dict(_CATCHUP)


def _trade_calendar(bars_map: dict) -> list:
    """交易日历：取候选池众数票的日期序列（K 线源即交易所日历，不自造）。

    只留 <= 今天的部分，升序。
    """
    ref = max(bars_map.values(), key=len) if bars_map else []
    days = sorted({str(b.get("d", ""))[:10] for b in ref
                   if b.get("d") and str(b["d"])[:10] <= _today()})
    return days


def catch_up(con: sqlite3.Connection, progress=None, force: bool = False) -> dict:
    """缺日补跑：把「最后一次预测日」到「最近已收盘交易日」之间漏掉的每个交易日补齐。

    核心是数据隔离的逐日链条（预测与验证严格分开）：
      对每个缺失交易日 d（按时间升序）：
        1) generate(run_date=d)：asof 截断——K 线只用到 d 收盘、龙虎榜/外部参考只取
           d 及以前发布的数据，**预测时看不见 d 之后的任何真实走势**；
        2) review()：用 d 之后已经走出来的**实际** K 线复盘历史预测（T+3/T+5/T+10），
           这一步是"验证"，只更新 ret/bench/hit 等后验字段，不改预测内容本身；
        3) generate 内部 rescore()：把刚验证完的命中率纳入权重，供下一天的预测使用。
      即：用 d 的数据预测 d+1 → 用 d+1 实际走势验证 → 权重微调 → 预测 d+2。逐日滚动。
    """
    with _CATCH_LOCK:
        if _CATCHUP["running"]:
            return {"ok": False, "error": "补跑已在进行中", "status": _catchup_status()}
        _CATCHUP.update(running=True, stage="准备", total=0, done=0, dates=[],
                        current=None, error=None, finished_at=None)
    try:
        ensure_tables(con)
        # 1) 交易日历（顺带把 K 线刷新到最新）
        #    【最新交易日锚点 · 硬保证】每日预测 / K 线刷新一律以"动态计算的最新交易日"为绝对上界，
        #    不用裸 today()，也不信 K 线缓存里可能滞后的旧日期（如国庆后 K 线停在节前 09-30）。
        #    anchor = _latest_trade_date_anchor()（收盘后含今日，盘中回退到昨日之前），
        #    永不依赖缓存默认值或 host 时钟的裸 today()。
        _CATCHUP["stage"] = "刷新K线"
        codes, _names, _b = SS.universe(POOL_LIMIT)
        if not codes:
            raise RuntimeError("候选池为空")

        # 锚点 = 真实最近【已收盘】交易日（收盘后含今日，盘中回退到昨日之前）。
        real_td = _latest_trade_date_anchor()
        # 同天对账：若"真实最新交易日"的预测尚未生成 → 强制刷新 K 线到最新，把预测补齐到当天。
        # 即使错过 9:15–9:35 现场窗口也没关系：只要当天预测还缺，收盘后这一轮就会 force 补上。
        row0 = con.execute("SELECT MAX(run_date) AS m FROM prediction_run").fetchone()
        last_run0 = row0["m"] if row0 else None
        force_kline = bool(force) or not (last_run0 and last_run0 >= real_td)

        kc = load_kcache()
        bars_map = fetch_bars(codes, kc, force=force_kline)
        bars_map = {c: v for c, v in bars_map.items() if len(v) >= MIN_BARS}
        cal = _trade_calendar(bars_map)
        if not cal:
            raise RuntimeError("交易日历为空（K 线不可用）")
        # target_end 一律以"真实最新交易日"为绝对上界——绝不超过它，也绝不用早于它的缓存/默认值。
        target_end = cal[-1]
        pushed_today = False
        if real_td > target_end:
            # K 线日历落后真实交易日（源尚未刷新，如长假后首日上午）→ 以真实交易日兜底目标日，
            # 让当日预测真正被尝试生成；generate 内部 asof 截断保证不偷看未来。
            # 若当日完整数据尚未采集，下面会给该次运行打 caveat，不编当日数据。
            target_end = real_td
            pushed_today = True
            if real_td not in cal:
                cal = cal + [real_td]
        elif real_td < target_end:
            # K 线日历领先于真实交易日（异常：缓存混入未来/非交易日期）→ 截断到真实交易日，
            # 绝不用未来日期或休市日数据做预测。
            target_end = real_td
        else:
            target_end = cal[-1]
        if not target_end:
            raise RuntimeError("无已收盘交易日")
        # 2) 缺失日 = (last_run, target_end] 内的交易日
        row = con.execute("SELECT MAX(run_date) AS m FROM prediction_run").fetchone()
        last_run = row["m"] if row else None
        if last_run:
            missing = [d for d in cal if d > last_run and d <= target_end]
        else:
            missing = [d for d in cal if d <= target_end][-_CATCH_MAX:]   # 首跑只补最近 10 天
        missing = missing[-_CATCH_MAX:]
        # 兜底目标日（真实今天）可能不在 K 线日历里（源尚未刷新），确保它进入补跑队列，
        # 否则上面把 target_end 推到今天也只是空转——这正是 10-08 当晚补跑失效的卡点。
        if pushed_today and target_end not in missing and (not last_run or target_end > last_run):
            missing.append(target_end)
            missing.sort()
        with _CATCH_LOCK:
            _CATCHUP.update(total=len(missing), dates=list(missing), done=0)
        results = []
        for i, d in enumerate(missing):
            with _CATCH_LOCK:
                _CATCHUP.update(current=d, stage="生成 %s 预测（asof 隔离）" % d, done=i)
            if progress:
                progress("补跑", i, len(missing))
            # 2a) 生成 d 日预测（隔离：看不见 d 之后）
            g = generate(con, run_date=d, force=False)
            if not g.get("ok"):
                # 已存在（cached）也算过；真失败则记录并继续下一天
                results.append({"date": d, "generate": g})
                continue
            # 兜底生成的「真实今天」预测：今日完整数据可能尚未采集，
            # 如实打 caveat，不让人误判为一份完整成熟的当日预测。
            if pushed_today and d == target_end:
                try:
                    latest_bar = max([(b[-1].get("d") or "")[:10]
                                      for b in bars_map.values() if b], default="—") or "—"
                    cave = "⚠ 今日完整数据尚未采集，本预测基于截至 %s 的最新可用数据生成" % latest_bar
                    con.execute("UPDATE prediction_run SET note = note || ? WHERE run_date=?",
                                ("｜" + cave, d))
                    con.execute("UPDATE prediction_item SET caveat = ? WHERE run_date=?", (cave, d))
                    con.commit()
                except Exception:  # noqa: BLE001
                    pass
            # 2b) 复盘验证：用实际走势验证全部历史预测（含刚生成的 d 之前的）
            r = review(con)
            results.append({"date": d, "generate": g, "review": r})
        with _CATCH_LOCK:
            _CATCHUP.update(running=False, stage="完成", done=len(missing),
                            error=None, finished_at=_now())
        return {"ok": True, "target_end": target_end, "last_run": last_run,
                "caught": [r.get("date") for r in results], "results": results}
    except Exception as e:  # noqa: BLE001
        with _CATCH_LOCK:
            _CATCHUP.update(running=False, stage="失败", error=str(e)[:300],
                            finished_at=_now())
        return {"ok": False, "error": str(e)[:300], "status": _catchup_status()}


def maybe_daily(con: sqlite3.Connection) -> dict:
    """调度线程每轮调用（仿 board_flow.maybe_sample 的挂法）：交易日收盘后跑一次「复盘 + 生成」。

    - 只在 15:05 之后、非周末运行；同一天成功后只跑一次。
    - 先复盘（已到期窗口 + 权重更新）→ 再走 catch_up 补齐缺失交易日（含今天）。
    - ⚠️ 失败不再标记 done（旧版 bug：抓 K 线失败也标 done → 当天永远不再生成）；
      连续失败 3 次才停手（防限流死循环打爆数据源），第二天自动恢复。
    """
    now = datetime.now()
    if now.weekday() >= 5:
        return {"skip": "weekend"}
    if (now.hour, now.minute) < (15, 5):
        return {"skip": "pre_close"}
    today = now.strftime("%Y-%m-%d")
    if _DAILY_DONE.get("date") == today:
        return {"skip": "done"}
    out: dict = {"date": today, "attempt": (_DAILY_DONE.get("fail") or 0) + 1}
    try:
        out["review"] = review(con)
    except Exception as e:  # noqa: BLE001
        out["review_error"] = str(e)[:200]
    ok = True
    try:
        # 同天对账在 catch_up 内部完成：以"动态计算的最新交易日"为锚，若当天预测缺失则强制刷新 K 线补齐。
        out["catch_up"] = catch_up(con)
        if not out["catch_up"].get("ok"):
            ok = False
    except Exception as e:  # noqa: BLE001
        out["catch_up_error"] = str(e)[:200]
        ok = False
    if ok:
        _DAILY_DONE["date"] = today
        _DAILY_DONE["last"] = out
        _DAILY_DONE["fail"] = 0
    else:
        _DAILY_DONE["fail"] = (_DAILY_DONE.get("fail") or 0) + 1
        if _DAILY_DONE["fail"] >= 3:
            _DAILY_DONE["date"] = today   # 连续 3 次失败，当天不再自动重试
            _DAILY_DONE["last"] = out
    return out


def run_now(con: sqlite3.Connection, force: bool = True) -> dict:
    """手动触发（前端按钮 / API）：复盘 + 生成，忽略"已生成"跳过逻辑。"""
    out: dict = {}
    try:
        out["review"] = review(con, force=force)
    except Exception as e:  # noqa: BLE001
        out["review_error"] = str(e)[:300]
    try:
        out["generate"] = generate(con, force=force)
    except Exception as e:  # noqa: BLE001
        out["generate_error"] = str(e)[:300]
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="每日预测引擎")
    ap.add_argument("--generate", action="store_true", help="生成当日预测")
    ap.add_argument("--review", action="store_true", help="复盘已到期窗口")
    ap.add_argument("--backtest", type=int, default=0, help="技术面历史回放天数")
    ap.add_argument("--force", action="store_true", help="忽略缓存强制重算")
    args = ap.parse_args()
    c = connect()
    try:
        ensure_tables(c)
        if args.generate:
            print(json.dumps(generate(c, force=args.force), ensure_ascii=False, indent=2)[:2000])
        if args.review:
            print(json.dumps(review(c), ensure_ascii=False, indent=2)[:2000])
        if args.backtest:
            print(json.dumps(backtest(args.backtest), ensure_ascii=False, indent=2)[:4000])
    finally:
        c.close()
